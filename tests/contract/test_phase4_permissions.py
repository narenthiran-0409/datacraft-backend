import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.models import Connection, Dataset, ProfileRun, Schema, User

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("POST", f"/api/v1/datasets/{RANDOM_ID}/profile", {}),
    ("GET", "/api/v1/profile-runs", None),
    ("GET", f"/api/v1/profile-runs/{RANDOM_ID}", None),
    ("GET", f"/api/v1/datasets/{RANDOM_ID}/profile", None),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase4_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase4_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def profiled_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    """A real discovered dataset with one completed profile run, for the
    200-valid-request contract cases."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    table_name = f"dq_contract_{uuid.uuid4().hex[:8]}"
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, 'b')"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    discover_job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(discover_job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()

    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    yield dataset, db.get(ProfileRun, profile_run.id)

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_get_profile_runs_valid_request_returns_200(client: TestClient, admin_headers: dict, profiled_dataset) -> None:
    response = client.get("/api/v1/profile-runs", headers=admin_headers)
    assert response.status_code == 200


def test_get_profile_run_by_id_valid_request_returns_200(client: TestClient, admin_headers: dict, profiled_dataset) -> None:
    _, profile_run = profiled_dataset
    response = client.get(f"/api/v1/profile-runs/{profile_run.id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"


def test_get_dataset_latest_profile_valid_request_returns_200(client: TestClient, admin_headers: dict, profiled_dataset) -> None:
    dataset, _ = profiled_dataset
    response = client.get(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers)
    assert response.status_code == 200


def test_post_profile_valid_request_returns_202(client: TestClient, admin_headers: dict, profiled_dataset) -> None:
    dataset, _ = profiled_dataset
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        response = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={})
        assert response.status_code == 202
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_post_profile_oversized_sample_size_returns_422(client: TestClient, admin_headers: dict, profiled_dataset) -> None:
    dataset, _ = profiled_dataset
    response = client.post(
        f"/api/v1/datasets/{dataset.id}/profile",
        headers=admin_headers,
        json={"sample_size": settings.PROFILING_MAX_SAMPLE_SIZE + 1},
    )
    assert response.status_code == 422


def test_post_profile_full_scan_over_limit_returns_422(
    client: TestClient, admin_headers: dict, profiled_dataset, monkeypatch
) -> None:
    dataset, _ = profiled_dataset
    monkeypatch.setattr(settings, "PROFILING_MAX_FULL_SCAN_ROWS", 0)
    response = client.post(
        f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True}
    )
    assert response.status_code == 422

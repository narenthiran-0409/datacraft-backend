"""End-to-end: discover -> poll job to completion -> browse datasets/columns
(confirm no foreign_keys field on GET /datasets/{id}) -> configure a manual
key -> cancel-in-progress against a deliberately slow mocked discovery.

The first four steps go through the real HTTP API
(POST /connections/{id}/discover -> 202 job_id -> GET /jobs/{id} -> ...),
with Celery configured task_always_eager so .delay() executes inline
without needing a live worker process in this test environment. The
cancel-in-progress step can't be exercised through that same synchronous
path (there's nothing to cancel once eager execution has already
finished), so it directly drives app.modules.discovery.tasks.run_discovery
against a mocked, deliberately slow provider — matching the approved plan's
"direct Celery task invocation is acceptable for automated tests" allowance.
"""
import uuid
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import Connection, Dataset, Schema, User
from app.modules.jobs.service import JobsService


def test_full_discovery_flow(client: TestClient, admin_headers: dict, pg_connection: Connection) -> None:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        discover_response = client.post(f"/api/v1/connections/{pg_connection.id}/discover", headers=admin_headers)
        assert discover_response.status_code == 202
        job_id = discover_response.json()["id"]

        job_response = client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
        assert job_response.status_code == 200
        assert job_response.json()["status"] == "COMPLETED"

        schemas_response = client.get(
            f"/api/v1/schemas?connection_id={pg_connection.id}", headers=admin_headers
        )
        assert schemas_response.status_code == 200
        public_schema = next(s for s in schemas_response.json() if s["name"] == "public")

        datasets_response = client.get(
            f"/api/v1/datasets?schema_id={public_schema['id']}&search=users", headers=admin_headers
        )
        assert datasets_response.status_code == 200
        users_dataset = next(d for d in datasets_response.json()["items"] if d["name"] == "users")

        dataset_detail_response = client.get(
            f"/api/v1/datasets/{users_dataset['id']}", headers=admin_headers
        )
        assert dataset_detail_response.status_code == 200
        assert "foreign_keys" not in dataset_detail_response.json()

        columns_response = client.get(
            f"/api/v1/datasets/{users_dataset['id']}/columns", headers=admin_headers
        )
        assert columns_response.status_code == 200
        assert any(c["name"] == "id" and c["is_primary_key"] for c in columns_response.json())

        # discover a PK-less disposable table so we have something to
        # manually configure a key for.
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_manual_key_config_via_api(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from sqlalchemy import select, text

    from app.modules.discovery.tasks import run_discovery

    schema_name = f"dq_test_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE SCHEMA {schema_name}"))
        db.execute(text(f"CREATE TABLE {schema_name}.pkless (ext_id TEXT, note TEXT)"))
        db.commit()

        jobs_service = JobsService(db, redis_client)
        job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()

        pg_schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == schema_name)).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == pg_schema.id, Dataset.name == "pkless")).scalar_one()

        columns_response = client.get(f"/api/v1/datasets/{dataset.id}/columns", headers=admin_headers)
        ext_id_column = next(c for c in columns_response.json() if c["name"] == "ext_id")

        key_response = client.put(
            f"/api/v1/datasets/{dataset.id}/key-columns",
            headers=admin_headers,
            json={"columns": [{"column_id": ext_id_column["id"], "ordinal": 0}]},
        )
        assert key_response.status_code == 200
        assert key_response.json()["key_strategy"] == "SINGLE_COLUMN"
    finally:
        db.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        db.commit()


def test_cancel_in_progress_discovery(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    from app.modules.discovery import tasks as discovery_tasks

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )

    fake_provider = MagicMock()
    fake_provider.list_schemas.return_value = ["schema_a", "schema_b"]

    def slow_list_datasets(schema_name: str):
        if schema_name == "schema_a":
            # Simulate cancellation arriving while discovery is busy working
            # on the first schema (a deliberately slow step in a real run).
            jobs_service.cancel(job.id)
        return []

    fake_provider.list_datasets.side_effect = slow_list_datasets
    fake_provider.get_capabilities.return_value = MagicMock(supports_foreign_keys=False)
    fake_provider.close.return_value = None

    monkeypatch.setattr(discovery_tasks, "get_provider", lambda *a, **k: fake_provider)

    discovery_tasks.run_discovery(str(job.id))

    db.expire_all()
    completed_job = jobs_service.get(job.id)
    assert completed_job.status == "CANCELLED"

"""End-to-end: start profiling via the real HTTP API -> real (eager) Celery
execution -> poll job to completion -> retrieve via both
GET /profile-runs/{id} and GET /datasets/{id}/profile -> cancellation test
-> rerun test.

The main flow goes through the real HTTP API with Celery configured
task_always_eager, matching Phase 3's e2e pattern (no live worker process
in this test environment). The cancellation step directly drives
app.modules.profiling.tasks.run_profile against a mocked slow provider, for
the same reason Phase 3's discovery e2e test does.
"""
import uuid
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import Connection, Dataset, Schema, User
from app.modules.jobs.service import JobsService
from app.modules.profiling.service import ProfilingService


def _make_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str) -> Dataset:
    from app.modules.discovery.tasks import run_discovery

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, name TEXT, score NUMERIC)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a', 10), (2, 'b', 20), (3, 'a', 10)"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    return db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()


def test_full_profiling_flow(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_profile_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name)

        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            start_response = client.post(
                f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True}
            )
            assert start_response.status_code == 202
            profile_run_body = start_response.json()
            job_id = profile_run_body["job_id"]
            profile_run_id = profile_run_body["id"]

            job_response = client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
            assert job_response.status_code == 200
            assert job_response.json()["status"] == "COMPLETED"

            run_by_id_response = client.get(f"/api/v1/profile-runs/{profile_run_id}", headers=admin_headers)
            assert run_by_id_response.status_code == 200
            assert run_by_id_response.json()["status"] == "COMPLETED"
            assert run_by_id_response.json()["quality_score"] is None
            assert run_by_id_response.json()["null_percentage"] is None

            latest_response = client.get(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers)
            assert latest_response.status_code == 200
            assert latest_response.json()["id"] == profile_run_id

            # No API route returns column-level profile data this phase
            # (only the 4 approved profiling endpoints exist) — verify the
            # column_profiles rows directly against the database instead.
            from app.db.models import ColumnProfile

            column_profiles = db.execute(
                select(ColumnProfile).where(ColumnProfile.profile_run_id == uuid.UUID(profile_run_id))
            ).scalars().all()
            assert len(column_profiles) == 3  # id, name, score
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_cancel_in_progress_profiling(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    from app.modules.profiling import tasks as profiling_tasks

    table_name = f"dq_e2e_cancel_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name)

        profile_run, job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )

        jobs_service = JobsService(db, redis_client)

        fake_provider = MagicMock()
        fake_provider.get_dataset_column_stats.return_value = {}
        fake_sample = MagicMock(rows=[{"id": 1, "name": "a", "score": 10}], is_full_scan=True)
        fake_provider.sample_rows.return_value = fake_sample
        fake_provider.close.return_value = None

        # Request cancellation "mid-run" by cancelling right after the
        # provider is constructed but before the per-column loop checks the
        # flag — simulating cancellation arriving while profiling is busy.
        def get_provider_and_cancel(*args, **kwargs):
            jobs_service.cancel(job.id)
            return fake_provider

        monkeypatch.setattr(profiling_tasks, "get_provider", get_provider_and_cancel)

        profiling_tasks.run_profile(str(job.id), str(profile_run.id))

        db.expire_all()
        completed_job = jobs_service.get(job.id)
        completed_run = db.get(type(profile_run), profile_run.id)
        assert completed_job.status == "CANCELLED"
        assert completed_run.status == "CANCELLED"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_rerun_after_completion(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_rerun_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name)

        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            first = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})
            assert first.status_code == 202
            first_id = first.json()["id"]

            second = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})
            assert second.status_code == 202
            second_id = second.json()["id"]

            assert first_id != second_id

            all_runs_response = client.get(f"/api/v1/profile-runs?dataset_id={dataset.id}", headers=admin_headers)
            assert all_runs_response.status_code == 200
            assert len(all_runs_response.json()) == 2
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

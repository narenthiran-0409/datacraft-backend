"""Integration tests for JobsService.cancel() — specifically the QUEUED
short-circuit path (job cancelled before any worker ever started it),
which used to only flip jobs.status and leave the linked run row
(validation_runs/profile_runs) stuck at QUEUED forever. Regression test
for the exact bug reported live: a real validation_run stayed QUEUED even
though its job had already moved to CANCELLED."""
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Connection, Dataset, ProfileRun, Schema, User, ValidationRun
from app.modules.jobs.service import JobsService
from app.modules.profiling.service import ProfilingService
from app.modules.validation.service import ValidationService


def _make_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str) -> Dataset:
    from app.modules.discovery.tasks import run_discovery

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a')"))
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


def test_cancelling_a_queued_validation_job_also_cancels_its_validation_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_cancel_val_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name)

        # Deliberately never call run_validation() — this row must stay
        # QUEUED, the exact state the bug depended on.
        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        assert validation_run.status == "QUEUED"
        assert job.status == "QUEUED"

        JobsService(db, redis_client).cancel(job.id)
        db.expire_all()

        cancelled_job = db.get(type(job), job.id)
        cancelled_run = db.get(ValidationRun, validation_run.id)
        assert cancelled_job.status == "CANCELLED"
        assert cancelled_run.status == "CANCELLED"  # the actual bug: this used to stay QUEUED forever
        assert cancelled_run.completed_at is not None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_cancelling_a_queued_profile_job_also_cancels_its_profile_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_cancel_prof_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name)

        profile_run, job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False,
        )
        assert profile_run.status == "QUEUED"

        JobsService(db, redis_client).cancel(job.id)
        db.expire_all()

        cancelled_job = db.get(type(job), job.id)
        cancelled_run = db.get(ProfileRun, profile_run.id)
        assert cancelled_job.status == "CANCELLED"
        assert cancelled_run.status == "CANCELLED"
        assert cancelled_run.completed_at is not None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

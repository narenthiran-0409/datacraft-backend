import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import InvalidTargetPathError, TargetAlreadyExistsError
from app.core.redis_client import get_redis_client
from app.db.models import PublishRun, StagingRecord, StagingRun, User
from app.modules.audit.service import AuditingService
from app.modules.jobs.service import JobsService
from app.modules.publishing.file_export import check_overwrite_allowed, resolve_target_path, write_row_snapshots
from app.modules.publishing.run_options import clear_overwrite, get_overwrite

_WRITE_FAILURE_ERRORS = (InvalidTargetPathError, TargetAlreadyExistsError, OSError)


@celery_app.task(name="publishing.run_publish")
def run_publish(job_id: str, publish_run_id: str) -> dict:
    db = SessionLocal()
    try:
        redis_client = get_redis_client()
        jobs_service = JobsService(db, redis_client)
        job = jobs_service.get(uuid.UUID(job_id))
        publish_run = db.get(PublishRun, uuid.UUID(publish_run_id))

        if (
            job.status in ("CANCELLED", "COMPLETED", "FAILED")
            or publish_run is None
            or publish_run.status in ("PUBLISHED", "FAILED")
        ):
            # Idempotency guard — same rationale as run_profile/run_validation:
            # a redelivered/duplicate Celery message, or a manual
            # re-invocation by job_id/publish_run_id alone, for a run
            # already resolved.
            if publish_run is not None:
                clear_overwrite(redis_client, publish_run.id)
            return {"status": job.status}

        staging_run = db.get(StagingRun, publish_run.staging_run_id)

        # The drift gate — re-checked here, not just at trigger time. If
        # this is the FIRST dequeue and drift exists unacknowledged, this
        # task exits cleanly WITHOUT marking anything running/failed,
        # leaving publish_runs at PENDING exactly as it was. The approved
        # resolution: POST /publish-runs/{id}/drift-acknowledge re-enqueues
        # this SAME task (same job_id/publish_run_id) once acknowledged,
        # giving it a second dequeue that passes this check and proceeds.
        if staging_run.has_source_drift and not publish_run.drift_acknowledged:
            return {"status": "PENDING", "reason": "drift_not_acknowledged"}

        actor = db.get(User, job.created_by) if job.created_by else None
        audit = AuditingService(db)

        jobs_service.mark_running(job.id)
        now = datetime.now(timezone.utc)
        publish_run.status = "PUBLISHING"
        publish_run.started_at = now
        publish_run.updated_at = now
        db.commit()

        overwrite = get_overwrite(redis_client, publish_run.id)

        try:
            row_snapshots = [
                r.row_snapshot
                for r in db.execute(
                    select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)
                ).scalars()
            ]
            target_path = resolve_target_path(publish_run.target_reference, settings.PUBLISH_FILE_EXPORT_DIRECTORY)
            check_overwrite_allowed(target_path, overwrite=overwrite)
            written_count = write_row_snapshots(target_path, row_snapshots)
        except _WRITE_FAILURE_ERRORS as exc:
            message = f"{type(exc).__name__}: {exc}"
            _fail(jobs_service, audit, job, publish_run, actor, message)
            db.commit()
            clear_overwrite(redis_client, publish_run.id)
            return {"status": "FAILED", "error": message}

        now = datetime.now(timezone.utc)
        publish_run.status = "PUBLISHED"
        publish_run.published_record_count = written_count
        publish_run.completed_at = now
        publish_run.updated_at = now
        jobs_service.mark_completed(job.id)

        duration_ms = (
            int((now - publish_run.started_at).total_seconds() * 1000) if publish_run.started_at else None
        )
        audit.record(
            actor=actor, action="publish_run.completed", entity_type="PUBLISH_RUN", entity_id=publish_run.id,
            metadata={
                "staging_run_id": str(staging_run.id), "target_type": publish_run.target_type,
                "published_record_count": written_count, "duration": duration_ms,
            },
        )
        db.commit()
        clear_overwrite(redis_client, publish_run.id)
        return {"status": "PUBLISHED", "published_record_count": written_count}
    finally:
        db.close()


def _fail(jobs_service, audit, job, publish_run, actor, message: str) -> None:
    jobs_service.mark_failed(job.id, message)
    now = datetime.now(timezone.utc)
    publish_run.status = "FAILED"
    publish_run.error_message = message
    publish_run.completed_at = now
    publish_run.updated_at = now
    audit.record(
        actor=actor, action="publish_run.failed", entity_type="PUBLISH_RUN", entity_id=publish_run.id,
        metadata={
            "staging_run_id": str(publish_run.staging_run_id), "target_type": publish_run.target_type,
        },
    )

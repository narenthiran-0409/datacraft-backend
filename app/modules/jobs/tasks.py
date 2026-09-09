from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.db.models import Job, ProfileRun, PublishRun, ValidationRun
from app.modules.audit.service import AuditingService

# Generic job_type -> linked "run" table registry. sweep_stale_jobs() looks
# up the matching run row by job_id and flips its status too, not just
# jobs.status. Extend this mapping (not the sweep logic itself) when a
# future phase adds a new async run table — no other change needed. This
# is the exact extension Phase 5 was designed to make (see the Phase 5
# plan's "Files/modules to add/change" section). Staging never needed an
# entry here (Phase 8 is fully synchronous, no Celery job ever tracks a
# staging_runs row).
#
# KNOWN LIMITATION (Phase 9 finding, not fixed — see the Phase 9 final
# report's Remaining Issues): the flip-to-FAILED check just below is
# hardcoded to `run_row.status in ("QUEUED", "RUNNING")`, which matches
# profile_runs'/validation_runs' in-flight vocabulary but NOT
# publish_runs', whose in-flight statuses are PENDING/PUBLISHING. A stale
# PUBLISH job will still have its own jobs.status correctly flipped to
# FAILED by this sweep, but the linked publish_runs row will NOT be
# auto-flipped alongside it. Fixing that would require changing this
# sweep's own logic, which is frozen Phase 1 behavior — flagged, not
# silently patched.
_RUN_TABLE_BY_JOB_TYPE = {
    "PROFILE_RUN": ProfileRun,
    "VALIDATION_RUN": ValidationRun,
    "PUBLISH": PublishRun,
}


@celery_app.task(name="jobs.sweep_stale_jobs")
def sweep_stale_jobs() -> dict:
    """Marks any RUNNING job whose updated_at (the heartbeat refreshed by
    JobsService.update_progress(), NOT started_at) is older than
    STALE_JOB_THRESHOLD_MINUTES as FAILED. Comparing against started_at
    would incorrectly kill large, legitimately still-progressing runs.

    Also updates the linked run table's status (looked up generically by
    job_id via _RUN_TABLE_BY_JOB_TYPE), e.g. a stale PROFILE_RUN job also
    flips its matching profile_runs.status to FAILED — not just jobs.status.

    This is a watchdog action with no originating human request, so its
    audit trail uses actor_type='SYSTEM', actor_id=NULL — distinct from
    discovery.completed/failed events, which use the originating user via
    job.created_by.
    """
    db = SessionLocal()
    try:
        threshold = datetime.now(timezone.utc) - timedelta(minutes=settings.STALE_JOB_THRESHOLD_MINUTES)
        stale_jobs = db.execute(
            select(Job).where(Job.status == "RUNNING", Job.updated_at < threshold)
        ).scalars().all()

        audit = AuditingService(db)
        now = datetime.now(timezone.utc)
        for job in stale_jobs:
            error_message = f"Marked stale: no progress for over {settings.STALE_JOB_THRESHOLD_MINUTES} minutes"

            job.status = "FAILED"
            job.completed_at = now
            job.updated_at = now
            job.error_message = error_message

            run_model = _RUN_TABLE_BY_JOB_TYPE.get(job.job_type)
            if run_model is not None:
                run_row = db.execute(select(run_model).where(run_model.job_id == job.id)).scalar_one_or_none()
                if run_row is not None and run_row.status in ("QUEUED", "RUNNING"):
                    run_row.status = "FAILED"
                    run_row.completed_at = now
                    run_row.error_message = error_message

            audit.record(
                actor=None,
                actor_type="SYSTEM",
                action="job.marked_stale",
                entity_type="JOB",
                entity_id=job.id,
                metadata={"threshold_minutes": settings.STALE_JOB_THRESHOLD_MINUTES},
            )

        db.commit()
        return {"swept": len(stale_jobs)}
    finally:
        db.close()

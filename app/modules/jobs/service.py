import uuid
from datetime import datetime, timezone

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import JobNotCancellableError, JobNotFoundError
from app.db.models import Job, ProfileRun, ValidationRun

_CANCEL_KEY_PREFIX = "job:"
_CANCEL_KEY_SUFFIX = ":cancel_requested"

# job_type -> linked "run" table, for the subset of run tables whose
# status vocabulary actually includes 'CANCELLED' — used only by cancel()
# below, to flip a linked run row (validation_runs/profile_runs) to
# CANCELLED when its job is cancelled before a worker ever picked it up
# (see cancel()'s QUEUED branch for why that path needs this at all).
# PUBLISH is deliberately absent here: publish_runs' status CHECK
# constraint is ('PENDING', 'PUBLISHING', 'PUBLISHED', 'FAILED') — no
# CANCELLED value exists, so writing one would violate the constraint,
# not just leave a stale value. This is a narrower, cancel-specific
# registry — app.modules.jobs.tasks.sweep_stale_jobs's own
# _RUN_TABLE_BY_JOB_TYPE additionally includes PUBLISH for a different
# reason (documented there): that check only ever matches PUBLISH's
# QUEUED/RUNNING-shaped condition when it can't fire, so it's a no-op for
# PUBLISH rather than a constraint violation. The two registries look
# similar but serve different write-safety requirements, so they're kept
# separate rather than shared.
_CANCELLABLE_RUN_TABLE_BY_JOB_TYPE = {
    "PROFILE_RUN": ProfileRun,
    "VALIDATION_RUN": ValidationRun,
}


def _cancel_key(job_id: uuid.UUID) -> str:
    return f"{_CANCEL_KEY_PREFIX}{job_id}{_CANCEL_KEY_SUFFIX}"


class JobsService:
    def __init__(self, db: Session, redis_client: Redis) -> None:
        self._db = db
        self._redis = redis_client

    def get(self, job_id: uuid.UUID) -> Job:
        job = self._db.get(Job, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")
        return job

    def find_in_flight(self, *, job_type: str, entity_type: str, entity_id: uuid.UUID) -> Job | None:
        return self._db.execute(
            select(Job).where(
                Job.job_type == job_type,
                Job.entity_type == entity_type,
                Job.entity_id == entity_id,
                Job.status.in_(("QUEUED", "RUNNING")),
            )
        ).scalars().first()

    def create(self, *, job_type: str, entity_type: str, entity_id: uuid.UUID, created_by: uuid.UUID) -> Job:
        job = Job(job_type=job_type, entity_type=entity_type, entity_id=entity_id, created_by=created_by)
        self._db.add(job)
        self._db.commit()
        self._db.refresh(job)
        return job

    def mark_running(self, job_id: uuid.UUID) -> Job:
        job = self.get(job_id)
        now = datetime.now(timezone.utc)
        job.status = "RUNNING"
        job.started_at = now
        job.updated_at = now
        self._db.commit()
        return job

    def update_progress(self, job_id: uuid.UUID, progress_percentage: int | None = None) -> Job:
        """Bumps updated_at unconditionally — this is the heartbeat the
        stale-job sweep (app.modules.jobs.tasks.sweep_stale_jobs) relies on."""
        job = self.get(job_id)
        if progress_percentage is not None:
            job.progress_percentage = progress_percentage
        job.updated_at = datetime.now(timezone.utc)
        self._db.commit()
        return job

    def mark_completed(
        self, job_id: uuid.UUID, error_message: str | None = None, result: dict | None = None
    ) -> Job:
        job = self.get(job_id)
        now = datetime.now(timezone.utc)
        job.status = "COMPLETED"
        job.completed_at = now
        job.updated_at = now
        job.error_message = error_message
        # Additive (migration 0019) — optional, defaults to None; every existing
        # caller that doesn't pass it behaves exactly as before.
        if result is not None:
            job.result = result
        self._db.commit()
        return job

    def mark_failed(self, job_id: uuid.UUID, error_message: str) -> Job:
        job = self.get(job_id)
        now = datetime.now(timezone.utc)
        job.status = "FAILED"
        job.completed_at = now
        job.updated_at = now
        job.error_message = error_message
        self._db.commit()
        return job

    def mark_cancelled(self, job_id: uuid.UUID) -> Job:
        job = self.get(job_id)
        now = datetime.now(timezone.utc)
        job.status = "CANCELLED"
        job.completed_at = now
        job.updated_at = now
        self._db.commit()
        return job

    def cancel(self, job_id: uuid.UUID) -> Job:
        job = self.get(job_id)
        if job.status not in ("QUEUED", "RUNNING"):
            raise JobNotCancellableError(f"Job {job_id} is {job.status} and cannot be cancelled")

        self._redis.set(_cancel_key(job_id), "1")

        if job.status == "QUEUED":
            # Never picked up by a worker yet — safe to mark cancelled
            # immediately, without waiting for a worker to notice the
            # cancel flag.
            #
            # BUG FIX: this used to only flip jobs.status, leaving the
            # linked run row (e.g. validation_runs) stuck at QUEUED
            # forever. The place that normally keeps both in sync is the
            # Celery task's own cancellation handling (run_validation()'s
            # _cancel() helper and its equivalent in profiling/tasks.py) —
            # but that code only runs once a worker actually starts
            # executing the task, and each task's own idempotency guard
            # (`if job.status in (CANCELLED, COMPLETED, FAILED): return`)
            # makes it a pure no-op if the message is ever delivered after
            # the fact. A job cancelled while still QUEUED never reaches
            # that code at all, so nothing was ever there to update the
            # run row for exactly this path.
            cancelled_job = self.mark_cancelled(job_id)

            run_model = _CANCELLABLE_RUN_TABLE_BY_JOB_TYPE.get(job.job_type)
            if run_model is not None:
                run_row = self._db.execute(
                    select(run_model).where(run_model.job_id == job.id)
                ).scalar_one_or_none()
                if run_row is not None and run_row.status == "QUEUED":
                    run_row.status = "CANCELLED"
                    run_row.completed_at = datetime.now(timezone.utc)
                    self._db.commit()

            return cancelled_job
        return job

    def is_cancel_requested(self, job_id: uuid.UUID) -> bool:
        return bool(self._redis.get(_cancel_key(job_id)))

    def clear_cancel_flag(self, job_id: uuid.UUID) -> None:
        self._redis.delete(_cancel_key(job_id))

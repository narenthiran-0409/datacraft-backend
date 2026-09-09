import uuid
from datetime import datetime, timezone

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import JobNotCancellableError, JobNotFoundError
from app.db.models import Job

_CANCEL_KEY_PREFIX = "job:"
_CANCEL_KEY_SUFFIX = ":cancel_requested"


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

    def mark_completed(self, job_id: uuid.UUID, error_message: str | None = None) -> Job:
        job = self.get(job_id)
        now = datetime.now(timezone.utc)
        job.status = "COMPLETED"
        job.completed_at = now
        job.updated_at = now
        job.error_message = error_message
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
            # Never picked up by a worker yet — safe to mark cancelled immediately.
            return self.mark_cancelled(job_id)
        return job

    def is_cancel_requested(self, job_id: uuid.UUID) -> bool:
        return bool(self._redis.get(_cancel_key(job_id)))

    def clear_cancel_flag(self, job_id: uuid.UUID) -> None:
        self._redis.delete(_cancel_key(job_id))

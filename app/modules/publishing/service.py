import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    NoDriftToAcknowledgeError,
    PublishAlreadyInProgressError,
    PublishRunNotFoundError,
    StagingRunNotEligibleError,
    StagingRunNotFoundError,
    TargetTypeNotSupportedError,
)
from app.core.redis_client import get_redis_client
from app.db.models import Job, PublishRun, StagingRun, User
from app.modules.audit.service import AuditingService
from app.modules.lineage.service import LineageService
from app.modules.publishing.run_options import set_overwrite

_SUPPORTED_TARGET_TYPES = frozenset({"FILE_EXPORT"})
_NON_FAILED_STATUSES = ("PENDING", "PUBLISHING", "PUBLISHED")


class PublishingService:
    """Strictly read-only with respect to Phase 1-8 data beyond
    staging_runs/staging_records reads and jobs writes: never writes to
    corrections, issues, approval_requests, approval_decisions,
    approval_decision_issues, staging_runs, or staging_records."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)
        self._lineage = LineageService(db)

    def get(self, publish_run_id: uuid.UUID) -> PublishRun:
        publish_run = self._db.get(PublishRun, publish_run_id)
        if publish_run is None:
            raise PublishRunNotFoundError(f"Publish run {publish_run_id} not found")
        return publish_run

    def trigger(
        self, staging_run_id: uuid.UUID, *, target_type: str, target_reference: str, overwrite: bool, actor: User,
    ) -> tuple[PublishRun, Job]:
        # Locked decision 4: row lock BEFORE checking for an existing
        # non-FAILED publish_runs row and BEFORE creating the new row.
        staging_run = self._db.execute(
            select(StagingRun).where(StagingRun.id == staging_run_id).with_for_update()
        ).scalar_one_or_none()
        if staging_run is None:
            raise StagingRunNotFoundError(f"Staging run {staging_run_id} not found")

        if staging_run.status != "READY" or not staging_run.is_current:
            raise StagingRunNotEligibleError(
                f"Staging run {staging_run_id} is not eligible for publishing "
                f"(status={staging_run.status}, is_current={staging_run.is_current})"
            )

        if target_type not in _SUPPORTED_TARGET_TYPES:
            raise TargetTypeNotSupportedError(
                f"target_type '{target_type}' is not supported in this phase (only FILE_EXPORT)"
            )

        existing = self._db.execute(
            select(PublishRun).where(
                PublishRun.staging_run_id == staging_run_id, PublishRun.status.in_(_NON_FAILED_STATUSES)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise PublishAlreadyInProgressError(
                f"Publish run {existing.id} ({existing.status}) already exists for this staging run"
            )

        # Created at PENDING regardless of drift — "it exists, but cannot
        # advance" per the frozen design's stated intent. The Celery task
        # itself re-checks the drift gate and refuses to proceed past
        # PENDING until drift_acknowledged is set.
        publish_run = PublishRun(
            staging_run_id=staging_run_id, status="PENDING", target_type=target_type,
            target_reference=target_reference, published_by=actor.id,
        )
        self._db.add(publish_run)
        self._db.flush()

        # Phase 10 touch point 8 (additive-only): STAGING_RUN -> PUBLISH_RUN,
        # written at row creation, regardless of eventual outcome.
        self._lineage.record_edge(
            "STAGING_RUN", staging_run_id, "PUBLISH_RUN", publish_run.id, "PUBLISHED_TO"
        )

        job = Job(
            job_type="PUBLISH", entity_type="STAGING_RUN", entity_id=staging_run_id, created_by=actor.id
        )
        self._db.add(job)
        self._db.flush()
        publish_run.job_id = job.id

        self._audit.record(
            actor=actor, action="publish_run.created", entity_type="PUBLISH_RUN", entity_id=publish_run.id,
            metadata={"staging_run_id": str(staging_run_id), "target_type": target_type},
        )
        self._db.commit()
        self._db.refresh(publish_run)
        self._db.refresh(job)

        # Redis-backed, not a Celery kwarg — must survive a SECOND dequeue
        # (the drift-acknowledge re-enqueue), which a task kwarg would not
        # reliably do. Written only after the DB commit succeeds.
        set_overwrite(get_redis_client(), publish_run.id, overwrite)

        # .delay() is dispatched by the caller (the API route), not here —
        # matching the established convention (ProfilingService.start_profiling,
        # ValidationService.start_validation): services create rows/jobs and
        # return them, routes own Celery dispatch.
        return publish_run, job

    def acknowledge_drift(self, publish_run_id: uuid.UUID, *, comment: str | None, actor: User) -> PublishRun:
        publish_run = self.get(publish_run_id)
        staging_run = self._db.get(StagingRun, publish_run.staging_run_id)

        if not staging_run.has_source_drift:
            raise NoDriftToAcknowledgeError(f"Staging run {staging_run.id} has no source drift to acknowledge")

        now = datetime.now(timezone.utc)
        publish_run.drift_acknowledged = True
        publish_run.drift_acknowledged_by = actor.id
        publish_run.drift_acknowledged_at = now
        publish_run.updated_at = now

        metadata = {"staging_run_id": str(staging_run.id)}
        if comment:
            # User-authored free text — included here (the one authorized
            # location, per the Phase 7 comment-handling precedent) but
            # never echoed anywhere else.
            metadata["comment"] = comment
        self._audit.record(
            actor=actor, action="publish_run.drift_acknowledged", entity_type="PUBLISH_RUN",
            entity_id=publish_run.id, metadata=metadata,
        )
        self._db.commit()
        self._db.refresh(publish_run)

        # Whether to re-enqueue the SAME task/job/publish_run — the approved
        # resolution to the PENDING-forever structural gap — is decided
        # here, but dispatched by the caller (the API route), matching the
        # .delay()-belongs-in-the-route convention. Only meaningful if the
        # run is still PENDING (blocked); a run that somehow already
        # progressed past PENDING has nothing left to unblock.
        return publish_run

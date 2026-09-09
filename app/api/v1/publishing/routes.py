import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.publishing.schemas import (
    DriftAcknowledgeRequest,
    PublishRunResponse,
    PublishTriggerRequest,
    PublishTriggerResponse,
)
from app.modules.publishing.service import PublishingService
from app.modules.publishing.tasks import run_publish

router = APIRouter(tags=["publishing"])


def get_publishing_service(db: Session = Depends(get_db)) -> PublishingService:
    return PublishingService(db)


@router.post("/staging-runs/{staging_run_id}/publish", response_model=PublishTriggerResponse, status_code=202)
def trigger_publish(
    staging_run_id: uuid.UUID,
    body: PublishTriggerRequest,
    service: PublishingService = Depends(get_publishing_service),
    current_user: User = Depends(require_permission("publish.execute")),
) -> PublishTriggerResponse:
    publish_run, job = service.trigger(
        staging_run_id,
        target_type=body.target_type,
        target_reference=body.target_reference,
        overwrite=body.overwrite,
        actor=current_user,
    )
    run_publish.delay(str(job.id), str(publish_run.id))
    return PublishTriggerResponse(job_id=job.id, publish_run_id=publish_run.id)


@router.post("/publish-runs/{publish_run_id}/drift-acknowledge", response_model=PublishRunResponse)
def acknowledge_drift(
    publish_run_id: uuid.UUID,
    body: DriftAcknowledgeRequest,
    db: Session = Depends(get_db),
    service: PublishingService = Depends(get_publishing_service),
    current_user: User = Depends(require_permission("publish.execute")),
) -> PublishRunResponse:
    acknowledged = service.acknowledge_drift(publish_run_id, comment=body.comment, actor=current_user)
    # Re-enqueue the SAME job/publish_run — the approved drift-acknowledge
    # lifecycle resolution. Only meaningful while still PENDING (blocked);
    # a run that already progressed past PENDING has nothing left to unblock.
    if acknowledged.status == "PENDING" and acknowledged.job_id is not None:
        run_publish.delay(str(acknowledged.job_id), str(acknowledged.id))
        # In task_always_eager mode (tests) this just ran synchronously in a
        # SEPARATE db session (the task opens its own SessionLocal()), so
        # `acknowledged`'s in-memory attributes are now stale — refresh
        # before building the response. Harmless no-op against a real
        # broker/worker, where the row won't have changed yet by request end.
        db.refresh(acknowledged)
    return PublishRunResponse.model_validate(acknowledged)


@router.get("/publish-runs/{publish_run_id}", response_model=PublishRunResponse)
def get_publish_run(
    publish_run_id: uuid.UUID,
    service: PublishingService = Depends(get_publishing_service),
    _: User = Depends(require_permission("publish.read")),
) -> PublishRunResponse:
    return PublishRunResponse.model_validate(service.get(publish_run_id))

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.v1.jobs.schemas import JobResponse
from app.core.database import get_db
from app.core.dependencies import get_current_user, get_user_permission_codes, require_permission
from app.core.exceptions import PermissionDeniedError
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.audit.service import AuditingService
from app.modules.jobs.service import JobsService

router = APIRouter(prefix="/jobs", tags=["jobs"])


def get_jobs_service(db: Session = Depends(get_db)) -> JobsService:
    return JobsService(db, get_redis_client())


# BUG FIX (found wiring the AI Orchestrator's async suggestion endpoints, which
# return only a job_id and require this endpoint to resolve it): the comment this
# replaced ("Phase 3's only job_type is DISCOVERY_RUN... revisit if a later phase
# introduces job types outside the discovery permission domain") was never revisited
# when Phase 12 added job_type="AI_SUGGESTION" — a user with ai.suggest but not
# discovery.run could trigger an AI suggestion job (202 + job_id) but then get a
# hard 403 reading its own job's status, with no way to know if it ever completed.
# GET only (not cancel — no AI job cancellation flow exists or is being added here).
@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: uuid.UUID,
    request: Request,
    service: JobsService = Depends(get_jobs_service),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> JobResponse:
    job = service.get(job_id)
    required_permission = "ai.suggest" if job.job_type == "AI_SUGGESTION" else "discovery.run"
    if required_permission not in get_user_permission_codes(db, current_user.id):
        AuditingService(db).record(
            actor=current_user,
            action="permission.denied",
            entity_type="USER",
            entity_id=current_user.id,
            metadata={"required_permission": required_permission, "path": str(request.url.path)},
        )
        db.commit()
        raise PermissionDeniedError(f"Missing required permission: {required_permission}")
    return JobResponse.model_validate(job)


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: uuid.UUID,
    service: JobsService = Depends(get_jobs_service),
    _: User = Depends(require_permission("discovery.run")),
) -> JobResponse:
    return JobResponse.model_validate(service.cancel(job_id))

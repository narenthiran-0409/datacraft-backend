import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1.jobs.schemas import JobResponse
from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.jobs.service import JobsService

router = APIRouter(prefix="/jobs", tags=["jobs"])


def get_jobs_service(db: Session = Depends(get_db)) -> JobsService:
    return JobsService(db, get_redis_client())


# Phase 3's only job_type is DISCOVERY_RUN, so job visibility/cancellation
# is gated on discovery.run — the same permission that creates jobs via
# POST /connections/{id}/discover. Revisit if a later phase introduces
# job types outside the discovery permission domain.


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: uuid.UUID,
    service: JobsService = Depends(get_jobs_service),
    _: User = Depends(require_permission("discovery.run")),
) -> JobResponse:
    return JobResponse.model_validate(service.get(job_id))


@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(
    job_id: uuid.UUID,
    service: JobsService = Depends(get_jobs_service),
    _: User = Depends(require_permission("discovery.run")),
) -> JobResponse:
    return JobResponse.model_validate(service.cancel(job_id))

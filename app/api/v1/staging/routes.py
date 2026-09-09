import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.staging.schemas import StagingRecordResponse, StagingRunResponse
from app.modules.staging.service import StagingService

router = APIRouter(tags=["staging"])


def get_staging_service(db: Session = Depends(get_db)) -> StagingService:
    return StagingService(db)


@router.post("/reviews/{review_id}/staging", response_model=StagingRunResponse, status_code=201)
def trigger_staging(
    review_id: uuid.UUID,
    service: StagingService = Depends(get_staging_service),
    current_user: User = Depends(require_permission("staging.create")),
) -> StagingRunResponse:
    return StagingRunResponse.model_validate(service.trigger(review_id, current_user))


@router.get("/staging-runs/{staging_run_id}", response_model=StagingRunResponse)
def get_staging_run(
    staging_run_id: uuid.UUID,
    service: StagingService = Depends(get_staging_service),
    _: User = Depends(require_permission("staging.read")),
) -> StagingRunResponse:
    return StagingRunResponse.model_validate(service.get(staging_run_id))


@router.get("/staging-runs/{staging_run_id}/records", response_model=list[StagingRecordResponse])
def list_staging_records(
    staging_run_id: uuid.UUID,
    drift_only: bool = Query(default=False),
    service: StagingService = Depends(get_staging_service),
    _: User = Depends(require_permission("staging.read")),
) -> list[StagingRecordResponse]:
    return [
        StagingRecordResponse.model_validate(r)
        for r in service.list_records(staging_run_id, drift_only=drift_only)
    ]

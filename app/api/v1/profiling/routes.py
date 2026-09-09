import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import ProfileRunNotFoundError
from app.core.redis_client import get_redis_client
from app.db.models import User
from app.modules.profiling.schemas import ProfileRunCreateRequest, ProfileRunResponse
from app.modules.profiling.service import ProfilingService
from app.modules.profiling.tasks import run_profile

router = APIRouter(tags=["profiling"])


def get_profiling_service(db: Session = Depends(get_db)) -> ProfilingService:
    return ProfilingService(db, get_redis_client())


@router.post("/datasets/{dataset_id}/profile", response_model=ProfileRunResponse, status_code=202)
def start_profiling(
    dataset_id: uuid.UUID,
    payload: ProfileRunCreateRequest,
    service: ProfilingService = Depends(get_profiling_service),
    current_user: User = Depends(require_permission("profiling.run")),
) -> ProfileRunResponse:
    profile_run, job = service.start_profiling(
        actor=current_user,
        dataset_id=dataset_id,
        sample_size=payload.sample_size,
        full_scan=payload.full_scan,
        include_top_values=payload.include_top_values,
    )
    run_profile.delay(str(job.id), str(profile_run.id))
    return ProfileRunResponse.model_validate(profile_run)


@router.get("/profile-runs", response_model=list[ProfileRunResponse])
def list_profile_runs(
    dataset_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    service: ProfilingService = Depends(get_profiling_service),
    _: User = Depends(require_permission("metadata.read")),
) -> list[ProfileRunResponse]:
    return [ProfileRunResponse.model_validate(r) for r in service.list_profile_runs(dataset_id=dataset_id, status=status)]


@router.get("/profile-runs/{profile_run_id}", response_model=ProfileRunResponse)
def get_profile_run(
    profile_run_id: uuid.UUID,
    service: ProfilingService = Depends(get_profiling_service),
    _: User = Depends(require_permission("metadata.read")),
) -> ProfileRunResponse:
    return ProfileRunResponse.model_validate(service.get_profile_run(profile_run_id))


@router.get("/datasets/{dataset_id}/profile", response_model=ProfileRunResponse)
def get_latest_dataset_profile(
    dataset_id: uuid.UUID,
    service: ProfilingService = Depends(get_profiling_service),
    _: User = Depends(require_permission("metadata.read")),
) -> ProfileRunResponse:
    latest = service.get_latest_completed_run(dataset_id)
    if latest is None:
        raise ProfileRunNotFoundError(f"No completed profile run found for dataset {dataset_id}")
    return ProfileRunResponse.model_validate(latest)

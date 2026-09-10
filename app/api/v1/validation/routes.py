import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import ValidationRunNotFoundError
from app.db.models import User
from app.modules.validation.schemas import (
    ValidationFailureListResponse,
    ValidationFailureResponse,
    ValidationRunCreateRequest,
    ValidationRunResponse,
)
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation

router = APIRouter(tags=["validation"])


def get_validation_service(db: Session = Depends(get_db)) -> ValidationService:
    return ValidationService(db)


@router.post("/datasets/{dataset_id}/validate", response_model=ValidationRunResponse, status_code=202)
def start_validation(
    dataset_id: uuid.UUID,
    payload: ValidationRunCreateRequest,
    service: ValidationService = Depends(get_validation_service),
    current_user: User = Depends(require_permission("validation.run")),
) -> ValidationRunResponse:
    validation_run, job = service.start_validation(
        actor=current_user, dataset_id=dataset_id, template_id=payload.template_id
    )
    run_validation.delay(str(job.id), str(validation_run.id))
    return ValidationRunResponse.model_validate(validation_run)


@router.get("/validation-runs", response_model=list[ValidationRunResponse])
def list_validation_runs(
    dataset_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    service: ValidationService = Depends(get_validation_service),
    _: User = Depends(require_permission("metadata.read")),
) -> list[ValidationRunResponse]:
    return [
        ValidationRunResponse.model_validate(r)
        for r in service.list_validation_runs(dataset_id=dataset_id, status=status)
    ]


@router.get("/validation-runs/{validation_run_id}", response_model=ValidationRunResponse)
def get_validation_run(
    validation_run_id: uuid.UUID,
    service: ValidationService = Depends(get_validation_service),
    _: User = Depends(require_permission("metadata.read")),
) -> ValidationRunResponse:
    return ValidationRunResponse.model_validate(service.get_validation_run(validation_run_id))


@router.get("/validation-runs/{validation_run_id}/failures", response_model=ValidationFailureListResponse)
def list_validation_failures(
    validation_run_id: uuid.UUID,
    severity: str | None = Query(default=None),
    column_id: uuid.UUID | None = Query(default=None),
    rule_assignment_id: uuid.UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    service: ValidationService = Depends(get_validation_service),
    _: User = Depends(require_permission("metadata.read")),
) -> ValidationFailureListResponse:
    """Row-level failure detail for a run — record_ref, which rule/column,
    and the actual failed_value/expected_value/reason, joined server-side.
    Same permission as every other validation-run read route
    (metadata.read) — validation.run gates *triggering* a run, not reading
    one; see the module docstring/report for why that distinction matters
    here."""
    items, total = service.list_failures(
        validation_run_id=validation_run_id,
        severity=severity,
        column_id=column_id,
        rule_assignment_id=rule_assignment_id,
        page=page,
        page_size=page_size,
    )
    return ValidationFailureListResponse(
        items=[ValidationFailureResponse(**item) for item in items], total=total, page=page, page_size=page_size
    )


@router.get("/datasets/{dataset_id}/validation", response_model=ValidationRunResponse)
def get_latest_dataset_validation(
    dataset_id: uuid.UUID,
    service: ValidationService = Depends(get_validation_service),
    _: User = Depends(require_permission("metadata.read")),
) -> ValidationRunResponse:
    latest = service.get_latest_completed_run(dataset_id)
    if latest is None:
        raise ValidationRunNotFoundError(f"No completed validation run found for dataset {dataset_id}")
    return ValidationRunResponse.model_validate(latest)

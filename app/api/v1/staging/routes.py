import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.staging.revalidation_service import StagedRevalidationService
from app.modules.staging.schemas import (
    MaterializedPreviewResponse,
    MaterializedPreviewRow,
    StagedRuleRevalidationResponse,
    StagingDestinationColumn,
    StagingDestinationResponse,
    StagingRecordResponse,
    StagingRunResponse,
)
from app.modules.staging.service import StagingService
from app.modules.staging.tasks import run_staging_materialization
from app.modules.validation.record_ref import generate_record_ref

router = APIRouter(tags=["staging"])


def get_staging_service(db: Session = Depends(get_db)) -> StagingService:
    return StagingService(db)


def get_revalidation_service(db: Session = Depends(get_db)) -> StagedRevalidationService:
    return StagedRevalidationService(db)


@router.post("/reviews/{review_id}/staging", response_model=StagingRunResponse, status_code=201)
def trigger_staging(
    review_id: uuid.UUID,
    service: StagingService = Depends(get_staging_service),
    current_user: User = Depends(require_permission("staging.create")),
) -> StagingRunResponse:
    staging_run = service.trigger(review_id, current_user)
    # Phase 4.12 — a successful audit-layer build (job_id populated by
    # trigger() itself) additionally kicks off the async full-dataset
    # materialization job. Matches the established .delay()-belongs-in-the-
    # route convention (ProfilingService/ValidationService/PublishingService
    # all dispatch from their routes, not their services). A FAILED audit
    # build never gets a job_id, so nothing is dispatched for it.
    if staging_run.job_id is not None:
        run_staging_materialization.delay(str(staging_run.job_id), str(staging_run.id))
    return StagingRunResponse.model_validate(staging_run)


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


@router.get(
    "/staging-records/{staging_record_id}/revalidation", response_model=list[StagedRuleRevalidationResponse]
)
def revalidate_staging_record(
    staging_record_id: uuid.UUID,
    service: StagedRevalidationService = Depends(get_revalidation_service),
    _: User = Depends(require_permission("staging.read")),
) -> list[StagedRuleRevalidationResponse]:
    """Phase 4.8 — computes fresh on every call; never reads the live
    source, never invokes AI, never writes anything."""
    reports = service.revalidate_staging_record(staging_record_id)
    return [
        StagedRuleRevalidationResponse(
            rule_assignment_id=r.rule_assignment_id, rule_id=r.rule_id, rule_type=r.rule_type,
            column_name=r.column_name, status=r.status, reason=r.reason, checked_value=r.checked_value,
        )
        for r in reports
    ]


@router.get("/staging-runs/{staging_run_id}/destination", response_model=StagingDestinationResponse)
def get_staging_destination(
    staging_run_id: uuid.UUID,
    service: StagingService = Depends(get_staging_service),
    _: User = Depends(require_permission("staging.read")),
) -> StagingDestinationResponse:
    """Phase 4.12 — real backend-owned destination metadata, replacing the
    frontend's buildMockStagingDestination(). Raises 409
    (STAGING_RUN_NOT_MATERIALIZED) for a historical or not-yet-materialized
    run rather than crashing."""
    metadata = service.get_destination_metadata(staging_run_id)
    run = metadata.staging_run
    return StagingDestinationResponse(
        staging_run_id=run.id, destination_schema=run.destination_schema, destination_table=run.destination_table,
        columns=[
            StagingDestinationColumn(
                name=c.name, normalized_data_type=c.normalized_data_type, staging_data_type=c.staging_data_type
            )
            for c in metadata.columns
        ],
        source_row_count=run.source_row_count, materialized_row_count=run.materialized_row_count,
        copied_row_count=run.copied_row_count, materialization_phase=run.materialization_phase,
        progress_percentage=run.progress_percentage, approved_correction_count=metadata.approved_correction_count,
        affected_row_count=metadata.affected_row_count,
    )


@router.get("/staging-runs/{staging_run_id}/preview", response_model=MaterializedPreviewResponse)
def preview_materialized_staging(
    staging_run_id: uuid.UUID,
    row_filter: Literal["ALL", "CHANGED", "UNCHANGED"] = Query(default="ALL", alias="filter"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: StagingService = Depends(get_staging_service),
    _: User = Depends(require_permission("staging.read")),
) -> MaterializedPreviewResponse:
    """Phase 4.12 — backend-authoritative preview of the MATERIALIZED
    physical staging table (never staging_records.row_snapshot, never a
    frontend record_ref heuristic against a raw source preview). Raises 409
    (STAGING_RUN_NOT_MATERIALIZED) for a historical/not-yet-materialized run."""
    result = service.preview_materialized(staging_run_id, row_filter=row_filter, limit=limit, offset=offset)
    rows = []
    for row_index, row in enumerate(result.rows):
        record_ref = (
            generate_record_ref(
                key_strategy=result.key_strategy, key_column_names_in_order=result.key_column_names,
                row=row, row_index=row_index,
            )
            if result.key_strategy != "ROW_INDEX_FALLBACK"
            else None
        )
        corrected_fields = result.corrected_fields_by_record_ref.get(record_ref, []) if record_ref else []
        rows.append(
            MaterializedPreviewRow(
                values=row, record_ref=record_ref, is_changed=bool(corrected_fields),
                corrected_fields=corrected_fields,
            )
        )
    return MaterializedPreviewResponse(
        columns=result.columns, rows=rows, total_rows=result.total_rows, limit=result.limit, offset=result.offset,
        has_more=result.has_more,
    )

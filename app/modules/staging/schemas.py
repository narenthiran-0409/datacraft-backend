import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class StagingRunResponse(BaseModel):
    id: uuid.UUID
    review_run_id: uuid.UUID
    dataset_id: uuid.UUID
    job_id: uuid.UUID | None
    attempt_number: int
    is_current: bool
    status: str
    record_count: int
    field_count: int
    has_source_drift: bool
    error_message: str | None
    created_by: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    # Phase 4.12 — materialized staging dataset progress. All None/0 for a
    # historical (pre-4.12) run or one whose audit-layer build failed (no
    # materialization job was ever enqueued for it). destination_table is
    # the authoritative "materialized?" signal — see StagingRunNotMaterializedError.
    destination_schema: str | None
    destination_table: str | None
    source_row_count: int | None
    materialized_row_count: int | None
    copied_row_count: int
    materialization_phase: str | None
    progress_percentage: int | None
    materialization_error: str | None

    model_config = {"from_attributes": True}


class StagingRecordResponse(BaseModel):
    id: uuid.UUID
    staging_run_id: uuid.UUID
    record_ref: str
    row_snapshot: dict
    corrected_fields: list
    source_row_hash_at_validation: str
    source_row_hash_at_staging: str | None
    source_drift_status: str
    source_drift_fields: list | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StagedRuleRevalidationResponse(BaseModel):
    """Phase 4.8 — computed fresh on every request, never persisted. See
    StagedRevalidationService's own docstring for why persistence is
    unnecessary: both inputs (row_snapshot, the targeted RuleVersion's
    definition) are already immutable once written."""

    rule_assignment_id: uuid.UUID
    rule_id: uuid.UUID
    rule_type: str
    column_name: str | None
    status: str
    reason: str | None
    checked_value: Any


class StagingDestinationColumn(BaseModel):
    name: str
    normalized_data_type: str
    staging_data_type: str


class StagingDestinationResponse(BaseModel):
    """Phase 4.12 — real backend-owned destination metadata, replacing the
    frontend's buildMockStagingDestination(). Raised as
    StagingRunNotMaterializedError (409) rather than returned for a
    historical/not-yet-materialized run."""

    staging_run_id: uuid.UUID
    destination_schema: str
    destination_table: str
    columns: list[StagingDestinationColumn]
    source_row_count: int | None
    materialized_row_count: int | None
    copied_row_count: int
    materialization_phase: str | None
    progress_percentage: int | None
    approved_correction_count: int
    affected_row_count: int


class MaterializedPreviewRow(BaseModel):
    values: dict[str, Any]
    record_ref: str | None
    is_changed: bool
    corrected_fields: list[dict[str, Any]]


class MaterializedPreviewResponse(BaseModel):
    columns: list[str]
    rows: list[MaterializedPreviewRow]
    total_rows: int
    limit: int
    offset: int
    has_more: bool

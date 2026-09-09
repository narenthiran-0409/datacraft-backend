import uuid
from datetime import datetime

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

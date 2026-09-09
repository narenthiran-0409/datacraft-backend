import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PublishTriggerRequest(BaseModel):
    # Plain str, not a Literal["FILE_EXPORT"] — WAREHOUSE_TABLE/SOURCE_TABLE/
    # API must be rejected by PublishingService.trigger() with a clear
    # TargetTypeNotSupportedError message (422), not Pydantic's generic
    # literal-mismatch validation error.
    target_type: str = Field(description="Only 'FILE_EXPORT' is supported in this phase.")
    target_reference: str
    overwrite: bool = False


class PublishRunResponse(BaseModel):
    id: uuid.UUID
    staging_run_id: uuid.UUID
    job_id: uuid.UUID | None
    status: str
    target_type: str
    target_reference: str | None
    published_record_count: int | None
    drift_acknowledged: bool
    drift_acknowledged_by: uuid.UUID | None
    drift_acknowledged_at: datetime | None
    error_message: str | None
    published_by: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class PublishTriggerResponse(BaseModel):
    job_id: uuid.UUID
    publish_run_id: uuid.UUID


class DriftAcknowledgeRequest(BaseModel):
    comment: str | None = None

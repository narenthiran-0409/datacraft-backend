import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ValidationRunCreateRequest(BaseModel):
    template_id: uuid.UUID | None = None


class ValidationRunResponse(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    template_id: uuid.UUID | None
    job_id: uuid.UUID | None
    status: str
    sample_size: int | None
    total_rows: int
    passed_rows: int
    warning_rows: int
    failed_rows: int
    quality_score: Decimal | None
    error_message: str | None
    triggered_by: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}

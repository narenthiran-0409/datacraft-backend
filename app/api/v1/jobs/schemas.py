import uuid
from datetime import datetime

from pydantic import BaseModel


class JobResponse(BaseModel):
    id: uuid.UUID
    job_type: str
    entity_type: str
    entity_id: uuid.UUID
    status: str
    progress_percentage: int | None
    error_message: str | None
    # Additive (migration 0019) — null for every job type except AI_SUGGESTION,
    # which sets {"ai_suggestion_id": "..."} (or "ai_suggestion_ids": [...] for
    # corrections) on completion. See app/db/models/jobs.py's Job.result comment.
    result: dict | None
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}

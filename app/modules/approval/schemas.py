import uuid
from datetime import datetime

from pydantic import BaseModel


class ApprovalRequestResponse(BaseModel):
    id: uuid.UUID
    review_run_id: uuid.UUID
    status: str
    affected_issue_count: int
    affected_record_count: int
    requested_by: uuid.UUID | None
    requested_at: datetime
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class ApprovalRequestDetailResponse(ApprovalRequestResponse):
    decided_count: int
    remaining_count: int


class ApprovalDecisionRequest(BaseModel):
    issue_ids: list[uuid.UUID]
    comment: str | None = None

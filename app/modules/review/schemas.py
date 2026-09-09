import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class ReviewRunCreateRequest(BaseModel):
    validation_run_id: uuid.UUID
    name: str | None = None


class ReviewRunResponse(BaseModel):
    id: uuid.UUID
    validation_run_id: uuid.UUID
    name: str | None
    status: str
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime | None
    archived_at: datetime | None

    model_config = {"from_attributes": True}


class IssueResponse(BaseModel):
    id: uuid.UUID
    review_run_id: uuid.UUID
    validation_failure_id: uuid.UUID
    column_id: uuid.UUID | None
    record_ref: str
    row_index: int
    original_value: str | None
    severity: str
    status: str
    assigned_reviewer_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class CorrectionSuggestionResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    source: str
    ai_suggestion_id: uuid.UUID | None
    suggested_value: str
    confidence: Decimal
    fix_type: str
    reasoning: str | None
    is_selected: bool
    selected_by: uuid.UUID | None
    selected_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CorrectionResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    correction_suggestion_id: uuid.UUID | None
    final_value: str | None
    value_source: str | None
    status: str
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


class GenerateSuggestionsResponse(BaseModel):
    generated_count: int
    issues_with_no_suggestion_count: int


class BulkActionRequest(BaseModel):
    issue_ids: list[uuid.UUID]
    action: Literal["skip", "reject"]


class BulkActionResponse(BaseModel):
    action: str
    issue_count: int


class EditSuggestionRequest(BaseModel):
    final_value: str


class RejectSuggestionRequest(BaseModel):
    reason: str | None = None


class CorrectIssueRequest(BaseModel):
    final_value: str

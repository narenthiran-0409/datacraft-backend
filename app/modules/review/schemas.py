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
    category: str
    fix_type: str
    reasoning: str | None
    is_selected: bool
    selected_by: uuid.UUID | None
    selected_at: datetime | None
    created_at: datetime
    # Phase 4.10 — expose the Phase 4.1 evidence columns (already persisted
    # by AISuggestionService.generate_corrections whenever advanced
    # inference actually ran for an issue — see app/db/models/review.py's
    # CorrectionSuggestion.strategy/evidence_detail docstring) so the
    # frontend can show "how" a suggestion was produced. No new computation:
    # both fields are read straight through via from_attributes. NULL for
    # every RULE_BASED suggestion and for any AI suggestion produced before
    # advanced inference was enabled / attempted for that issue.
    strategy: str | None = None
    evidence_detail: dict | None = None

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


class AITracePromptResponse(BaseModel):
    id: uuid.UUID
    key: str
    version_number: int


class AITraceUsageEntryResponse(BaseModel):
    id: uuid.UUID
    provider: str
    model: str
    prompt_version_id: uuid.UUID | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None
    status: str
    created_at: datetime


class AITraceResponse(BaseModel):
    """Phase 4.9 — audit-oriented, read-only. Never includes raw prompt
    body or raw model response, and never includes any credential/secret
    — see AITraceService's own docstring for exactly what this is built
    from."""

    correction_suggestion_id: uuid.UUID
    ai_suggestion_id: uuid.UUID | None
    is_llm_backed: bool
    linkage_status: str
    provider: str | None
    model: str | None
    prompt: AITracePromptResponse | None
    usage: list[AITraceUsageEntryResponse]

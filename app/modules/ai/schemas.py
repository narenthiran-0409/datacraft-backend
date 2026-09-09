import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class ChatRequest(BaseModel):
    conversation_id: uuid.UUID | None = None
    message: str


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message: ChatMessageResponse


class ConversationDetailResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    status: str
    created_at: datetime
    updated_at: datetime | None
    messages: list[ChatMessageResponse]


class ExplanationRequest(BaseModel):
    issue_id: uuid.UUID


class RunSummaryRequest(BaseModel):
    validation_run_id: uuid.UUID


class PrioritizationRequest(BaseModel):
    review_run_id: uuid.UUID


class ClusterRequest(BaseModel):
    review_run_id: uuid.UUID


class CorrectionSuggestionRequest(BaseModel):
    review_run_id: uuid.UUID


class AISuggestionTriggerResponse(BaseModel):
    job_id: uuid.UUID


class AISuggestionResponse(BaseModel):
    id: uuid.UUID
    suggestion_type: str
    source_context_type: str
    source_context_id: uuid.UUID
    content: dict
    confidence: Decimal | None
    provider: str
    model: str
    prompt_version_id: uuid.UUID
    conversation_id: uuid.UUID | None
    requested_by: uuid.UUID | None
    status: str
    created_at: datetime

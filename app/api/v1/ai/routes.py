import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import DatasetNotFoundError, ReviewRunNotFoundError, ValidationRunNotFoundError
from app.core.redis_client import get_redis_client
from app.db.models import Dataset, ReviewRun, User, ValidationRun
from app.modules.ai.chat_service import AIChatService
from app.modules.ai.schemas import (
    AISuggestionResponse,
    AISuggestionTriggerResponse,
    ChatMessageResponse,
    ChatRequest,
    ChatResponse,
    ClusterRequest,
    ConversationDetailResponse,
    CorrectionSuggestionRequest,
    ExplanationRequest,
    PrioritizationRequest,
    RuleDetectionRequest,
    RunSummaryRequest,
)
from app.modules.ai.suggestion_service import AISuggestionService
from app.modules.ai.tasks import (
    run_ai_cluster,
    run_ai_corrections,
    run_ai_prioritization,
    run_ai_run_summary,
    run_rule_detection,
)
from app.modules.jobs.service import JobsService

router = APIRouter(prefix="/ai", tags=["ai"])


def get_chat_service(db: Session = Depends(get_db)) -> AIChatService:
    return AIChatService(db)


def get_suggestion_service(db: Session = Depends(get_db)) -> AISuggestionService:
    return AISuggestionService(db)


def get_jobs_service(db: Session = Depends(get_db)) -> JobsService:
    return JobsService(db, get_redis_client())


# --- Chat (synchronous, ai.chat) --------------------------------------------


@router.post("/chat", response_model=ChatResponse)
def send_chat_message(
    body: ChatRequest,
    service: AIChatService = Depends(get_chat_service),
    current_user: User = Depends(require_permission("ai.chat")),
) -> ChatResponse:
    conversation, message = service.send_message(
        conversation_id=body.conversation_id, message=body.message, actor=current_user
    )
    return ChatResponse(
        conversation_id=conversation.id,
        message=ChatMessageResponse(id=message.id, role=message.role, content=message.content, created_at=message.created_at),
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: uuid.UUID,
    service: AIChatService = Depends(get_chat_service),
    current_user: User = Depends(require_permission("ai.chat")),
) -> ConversationDetailResponse:
    conversation, messages = service.get_conversation(conversation_id, current_user)
    return ConversationDetailResponse(
        id=conversation.id, user_id=conversation.user_id, title=conversation.title, status=conversation.status,
        created_at=conversation.created_at, updated_at=conversation.updated_at,
        messages=[
            ChatMessageResponse(id=m.id, role=m.role, content=m.content, created_at=m.created_at) for m in messages
        ],
    )


# --- Advisory suggestions (ai.suggest) --------------------------------------


@router.post("/suggestions/explanation", response_model=AISuggestionResponse)
def generate_explanation(
    body: ExplanationRequest,
    service: AISuggestionService = Depends(get_suggestion_service),
    current_user: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionResponse:
    suggestion = service.generate_explanation(body.issue_id, current_user)
    return AISuggestionResponse.model_validate(suggestion, from_attributes=True)


@router.post("/suggestions/run-summary", response_model=AISuggestionTriggerResponse, status_code=202)
def trigger_run_summary(
    body: RunSummaryRequest,
    db: Session = Depends(get_db),
    jobs_service: JobsService = Depends(get_jobs_service),
    current_user: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionTriggerResponse:
    if db.get(ValidationRun, body.validation_run_id) is None:
        raise ValidationRunNotFoundError(f"Validation run {body.validation_run_id} not found")
    job = jobs_service.create(
        job_type="AI_SUGGESTION", entity_type="VALIDATION_RUN", entity_id=body.validation_run_id,
        created_by=current_user.id,
    )
    run_ai_run_summary.delay(str(job.id), str(body.validation_run_id))
    return AISuggestionTriggerResponse(job_id=job.id)


@router.post("/suggestions/prioritization", response_model=AISuggestionTriggerResponse, status_code=202)
def trigger_prioritization(
    body: PrioritizationRequest,
    db: Session = Depends(get_db),
    jobs_service: JobsService = Depends(get_jobs_service),
    current_user: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionTriggerResponse:
    if db.get(ReviewRun, body.review_run_id) is None:
        raise ReviewRunNotFoundError(f"Review run {body.review_run_id} not found")
    job = jobs_service.create(
        job_type="AI_SUGGESTION", entity_type="REVIEW_RUN", entity_id=body.review_run_id,
        created_by=current_user.id,
    )
    run_ai_prioritization.delay(str(job.id), str(body.review_run_id))
    return AISuggestionTriggerResponse(job_id=job.id)


@router.post("/suggestions/cluster", response_model=AISuggestionTriggerResponse, status_code=202)
def trigger_cluster(
    body: ClusterRequest,
    db: Session = Depends(get_db),
    jobs_service: JobsService = Depends(get_jobs_service),
    current_user: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionTriggerResponse:
    if db.get(ReviewRun, body.review_run_id) is None:
        raise ReviewRunNotFoundError(f"Review run {body.review_run_id} not found")
    job = jobs_service.create(
        job_type="AI_SUGGESTION", entity_type="REVIEW_RUN", entity_id=body.review_run_id,
        created_by=current_user.id,
    )
    run_ai_cluster.delay(str(job.id), str(body.review_run_id))
    return AISuggestionTriggerResponse(job_id=job.id)


@router.post("/suggestions/corrections", response_model=AISuggestionTriggerResponse, status_code=202)
def trigger_corrections(
    body: CorrectionSuggestionRequest,
    db: Session = Depends(get_db),
    jobs_service: JobsService = Depends(get_jobs_service),
    current_user: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionTriggerResponse:
    if db.get(ReviewRun, body.review_run_id) is None:
        raise ReviewRunNotFoundError(f"Review run {body.review_run_id} not found")
    job = jobs_service.create(
        job_type="AI_SUGGESTION", entity_type="REVIEW_RUN", entity_id=body.review_run_id,
        created_by=current_user.id,
    )
    run_ai_corrections.delay(str(job.id), str(body.review_run_id))
    return AISuggestionTriggerResponse(job_id=job.id)


@router.post("/suggestions/rule-detection", response_model=AISuggestionTriggerResponse, status_code=202)
def trigger_rule_detection(
    body: RuleDetectionRequest,
    db: Session = Depends(get_db),
    jobs_service: JobsService = Depends(get_jobs_service),
    current_user: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionTriggerResponse:
    """Whole-dataset candidate-rule detection: a fast pattern-matching pass
    over every column, falling back to one batched LLM call for whatever
    it wasn't confident about. Every candidate rule it produces lands as
    status=PENDING_REVIEW — see POST /rules/{rule_id}/promote (or
    /dismiss) for the explicit human step that's required before any of
    them can affect a validation run. Poll GET /jobs/{job_id} for the
    result summary (counts and rule ids by detection method)."""
    if db.get(Dataset, body.dataset_id) is None:
        raise DatasetNotFoundError(f"Dataset {body.dataset_id} not found")
    job = jobs_service.create(
        job_type="AI_SUGGESTION", entity_type="DATASET", entity_id=body.dataset_id, created_by=current_user.id,
    )
    run_rule_detection.delay(str(job.id), str(body.dataset_id))
    return AISuggestionTriggerResponse(job_id=job.id)


@router.get("/suggestions/{suggestion_id}", response_model=AISuggestionResponse)
def get_suggestion(
    suggestion_id: uuid.UUID,
    service: AISuggestionService = Depends(get_suggestion_service),
    _: User = Depends(require_permission("ai.suggest")),
) -> AISuggestionResponse:
    return AISuggestionResponse.model_validate(service.get(suggestion_id), from_attributes=True)

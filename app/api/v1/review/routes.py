import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.core.exceptions import IssueNotFoundError
from app.db.models import CorrectionSuggestion, Issue, User
from app.modules.ai.trace_service import AITraceService
from app.modules.review.decision_service import CorrectionDecisionService
from app.modules.review.schemas import (
    AITracePromptResponse,
    AITraceResponse,
    AITraceUsageEntryResponse,
    BulkActionRequest,
    BulkActionResponse,
    CorrectIssueRequest,
    CorrectionResponse,
    CorrectionSuggestionResponse,
    EditSuggestionRequest,
    GenerateSuggestionsResponse,
    IssueResponse,
    RejectSuggestionRequest,
    ReviewRunCreateRequest,
    ReviewRunResponse,
)
from app.modules.review.service import ReviewService
from app.modules.review.suggestion_service import SuggestionService

router = APIRouter(tags=["review"])


def get_review_service(db: Session = Depends(get_db)) -> ReviewService:
    return ReviewService(db)


def get_suggestion_service(db: Session = Depends(get_db)) -> SuggestionService:
    return SuggestionService(db)


def get_decision_service(db: Session = Depends(get_db)) -> CorrectionDecisionService:
    return CorrectionDecisionService(db)


def get_ai_trace_service(db: Session = Depends(get_db)) -> AITraceService:
    return AITraceService(db)


@router.get("/reviews", response_model=list[ReviewRunResponse])
def list_reviews(
    validation_run_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    service: ReviewService = Depends(get_review_service),
    _: User = Depends(require_permission("review.read")),
) -> list[ReviewRunResponse]:
    return [
        ReviewRunResponse.model_validate(r)
        for r in service.list_review_runs(validation_run_id=validation_run_id, status=status)
    ]


@router.post("/reviews", response_model=ReviewRunResponse, status_code=201)
def create_review(
    payload: ReviewRunCreateRequest,
    service: ReviewService = Depends(get_review_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> ReviewRunResponse:
    review_run = service.create_from_validation_run(
        validation_run_id=payload.validation_run_id, name=payload.name, actor=current_user
    )
    return ReviewRunResponse.model_validate(review_run)


@router.get("/reviews/{review_id}", response_model=ReviewRunResponse)
def get_review(
    review_id: uuid.UUID,
    service: ReviewService = Depends(get_review_service),
    _: User = Depends(require_permission("review.read")),
) -> ReviewRunResponse:
    return ReviewRunResponse.model_validate(service.get(review_id))


@router.get("/reviews/{review_id}/issues", response_model=list[IssueResponse])
def list_review_issues(
    review_id: uuid.UUID,
    status: str | None = Query(default=None),
    service: ReviewService = Depends(get_review_service),
    _: User = Depends(require_permission("review.read")),
) -> list[IssueResponse]:
    return [IssueResponse.model_validate(i) for i in service.list_issues(review_id, status=status)]


@router.get("/reviews/{review_id}/suggestions", response_model=list[CorrectionSuggestionResponse])
def list_review_suggestions(
    review_id: uuid.UUID,
    db: Session = Depends(get_db),
    review_service: ReviewService = Depends(get_review_service),
    _: User = Depends(require_permission("review.read")),
) -> list[CorrectionSuggestionResponse]:
    review_service.get(review_id)  # 404 if the review run doesn't exist
    rows = db.execute(
        select(CorrectionSuggestion)
        .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
        .where(Issue.review_run_id == review_id)
    ).scalars()
    return [CorrectionSuggestionResponse.model_validate(r) for r in rows]


@router.post("/reviews/{review_id}/generate-suggestions", response_model=GenerateSuggestionsResponse)
def generate_suggestions(
    review_id: uuid.UUID,
    service: SuggestionService = Depends(get_suggestion_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> GenerateSuggestionsResponse:
    result = service.generate_for_review_run(review_id, current_user)
    return GenerateSuggestionsResponse(**result)


@router.post("/reviews/{review_id}/bulk-action", response_model=BulkActionResponse)
def bulk_action(
    review_id: uuid.UUID,
    payload: BulkActionRequest,
    service: CorrectionDecisionService = Depends(get_decision_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> BulkActionResponse:
    result = service.bulk_action(
        review_run_id=review_id, issue_ids=payload.issue_ids, action=payload.action, actor=current_user
    )
    return BulkActionResponse(**result)


@router.post("/reviews/{review_id}/archive", response_model=ReviewRunResponse)
def archive_review(
    review_id: uuid.UUID,
    service: ReviewService = Depends(get_review_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> ReviewRunResponse:
    return ReviewRunResponse.model_validate(service.archive(review_id, current_user))


@router.post("/reviews/{review_id}/restore", response_model=ReviewRunResponse)
def restore_review(
    review_id: uuid.UUID,
    service: ReviewService = Depends(get_review_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> ReviewRunResponse:
    return ReviewRunResponse.model_validate(service.restore(review_id, current_user))


@router.get("/issues/{issue_id}", response_model=IssueResponse)
def get_issue(
    issue_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("review.read")),
) -> IssueResponse:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise IssueNotFoundError(f"Issue {issue_id} not found")
    return IssueResponse.model_validate(issue)


@router.post("/suggestions/{suggestion_id}/accept", response_model=CorrectionResponse)
def accept_suggestion(
    suggestion_id: uuid.UUID,
    service: CorrectionDecisionService = Depends(get_decision_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> CorrectionResponse:
    return CorrectionResponse.model_validate(service.accept(suggestion_id, current_user))


@router.post("/suggestions/{suggestion_id}/edit", response_model=CorrectionResponse)
def edit_suggestion(
    suggestion_id: uuid.UUID,
    payload: EditSuggestionRequest,
    service: CorrectionDecisionService = Depends(get_decision_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> CorrectionResponse:
    return CorrectionResponse.model_validate(service.edit(suggestion_id, payload.final_value, current_user))


@router.post("/suggestions/{suggestion_id}/reject", response_model=CorrectionResponse)
def reject_suggestion(
    suggestion_id: uuid.UUID,
    payload: RejectSuggestionRequest,
    service: CorrectionDecisionService = Depends(get_decision_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> CorrectionResponse:
    return CorrectionResponse.model_validate(service.reject(suggestion_id, payload.reason, current_user))


@router.post("/issues/{issue_id}/correct", response_model=CorrectionResponse)
def correct_issue_directly(
    issue_id: uuid.UUID,
    payload: CorrectIssueRequest,
    service: CorrectionDecisionService = Depends(get_decision_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> CorrectionResponse:
    return CorrectionResponse.model_validate(service.correct_directly(issue_id, payload.final_value, current_user))


@router.get("/suggestions/{suggestion_id}/ai-trace", response_model=AITraceResponse)
def get_suggestion_ai_trace(
    suggestion_id: uuid.UUID,
    service: AITraceService = Depends(get_ai_trace_service),
    _: User = Depends(require_permission("review.read")),
) -> AITraceResponse:
    """Phase 4.9 — read-only audit trace. Never invokes AI; never
    returns raw prompt body, raw model response, or any credential."""
    trace = service.get_trace(suggestion_id)
    return AITraceResponse(
        correction_suggestion_id=trace.correction_suggestion_id, ai_suggestion_id=trace.ai_suggestion_id,
        is_llm_backed=trace.is_llm_backed, linkage_status=trace.linkage_status, provider=trace.provider,
        model=trace.model,
        prompt=(
            AITracePromptResponse(id=trace.prompt.id, key=trace.prompt.key, version_number=trace.prompt.version_number)
            if trace.prompt is not None
            else None
        ),
        usage=[
            AITraceUsageEntryResponse(
                id=u.id, provider=u.provider, model=u.model, prompt_version_id=u.prompt_version_id,
                input_tokens=u.input_tokens, output_tokens=u.output_tokens, total_tokens=u.total_tokens,
                latency_ms=u.latency_ms, status=u.status, created_at=u.created_at,
            )
            for u in trace.usage
        ],
    )

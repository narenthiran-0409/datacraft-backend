import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import require_permission
from app.db.models import User
from app.modules.approval.schemas import (
    ApprovalDecisionRequest,
    ApprovalRequestDetailResponse,
    ApprovalRequestResponse,
)
from app.modules.approval.service import ApprovalService

router = APIRouter(tags=["approval"])


def get_approval_service(db: Session = Depends(get_db)) -> ApprovalService:
    return ApprovalService(db)


@router.get("/approvals", response_model=list[ApprovalRequestResponse])
def list_approvals(
    status: str | None = Query(default=None),
    review_run_id: uuid.UUID | None = Query(default=None),
    service: ApprovalService = Depends(get_approval_service),
    _: User = Depends(require_permission("approval.read")),
) -> list[ApprovalRequestResponse]:
    return [
        ApprovalRequestResponse.model_validate(r)
        for r in service.list_requests(status=status, review_run_id=review_run_id)
    ]


@router.post("/reviews/{review_id}/submit-approval", response_model=ApprovalRequestResponse, status_code=201)
def submit_approval(
    review_id: uuid.UUID,
    service: ApprovalService = Depends(get_approval_service),
    current_user: User = Depends(require_permission("review.edit")),
) -> ApprovalRequestResponse:
    """No issue_ids[] parameter — locked design item 1. Scope is always
    every currently-resolved issue in the review run, computed fresh."""
    return ApprovalRequestResponse.model_validate(service.submit(review_id, current_user))


@router.get("/approvals/{approval_id}", response_model=ApprovalRequestDetailResponse)
def get_approval(
    approval_id: uuid.UUID,
    service: ApprovalService = Depends(get_approval_service),
    _: User = Depends(require_permission("approval.read")),
) -> ApprovalRequestDetailResponse:
    approval_request, decided_count, remaining_count = service.get_detail(approval_id)
    return ApprovalRequestDetailResponse(
        **ApprovalRequestResponse.model_validate(approval_request).model_dump(),
        decided_count=decided_count, remaining_count=remaining_count,
    )


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalRequestResponse)
def approve(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    service: ApprovalService = Depends(get_approval_service),
    current_user: User = Depends(require_permission("approval.decide")),
) -> ApprovalRequestResponse:
    result = service.decide(
        approval_id, decision="APPROVE", issue_ids=payload.issue_ids, comment=payload.comment, actor=current_user
    )
    return ApprovalRequestResponse.model_validate(result)


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalRequestResponse)
def reject(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    service: ApprovalService = Depends(get_approval_service),
    current_user: User = Depends(require_permission("approval.decide")),
) -> ApprovalRequestResponse:
    result = service.decide(
        approval_id, decision="REJECT", issue_ids=payload.issue_ids, comment=payload.comment, actor=current_user
    )
    return ApprovalRequestResponse.model_validate(result)

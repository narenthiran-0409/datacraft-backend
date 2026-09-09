import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    ApprovalRequestAlreadyPendingError,
    ApprovalRequestNotFoundError,
    IssueNotInApprovalScopeError,
    NoResolvedIssuesError,
    ReviewRunNotFoundError,
    ReviewRunNotReadyForSubmissionError,
)
from app.db.models import ApprovalDecision, ApprovalDecisionIssue, ApprovalRequest, Correction, Issue, ReviewRun, User
from app.modules.audit.service import AuditingService
from app.modules.lineage.service import LineageService
from app.modules.approval.status_logic import recompute_approval_request_status, resolved_scope_from_rows

_RESOLVED_CORRECTION_STATUSES = ("ACCEPTED", "EDITED")
_OPEN_APPROVAL_STATUSES = ("PENDING", "PARTIALLY_APPROVED")


class ApprovalService:
    """No jobs table involvement anywhere in this module — every operation
    is a single, fully synchronous DB transaction."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)
        self._lineage = LineageService(db)

    def get(self, approval_request_id: uuid.UUID) -> ApprovalRequest:
        approval_request = self._db.get(ApprovalRequest, approval_request_id)
        if approval_request is None:
            raise ApprovalRequestNotFoundError(f"Approval request {approval_request_id} not found")
        return approval_request

    def list_requests(
        self, *, status: str | None, review_run_id: uuid.UUID | None
    ) -> list[ApprovalRequest]:
        stmt = select(ApprovalRequest)
        if status is not None:
            stmt = stmt.where(ApprovalRequest.status == status)
        if review_run_id is not None:
            stmt = stmt.where(ApprovalRequest.review_run_id == review_run_id)
        stmt = stmt.order_by(ApprovalRequest.requested_at.desc())
        return list(self._db.execute(stmt).scalars())

    def _resolved_scope(self, review_run_id: uuid.UUID) -> tuple[list[uuid.UUID], int]:
        """The frozen resolved-issue definition (locked design item 1):
        every issue in the review run whose corrections row has
        final_value IS NOT NULL AND status IN ('ACCEPTED','EDITED'),
        evaluated fresh — never persisted as a list. affected_record_count
        is the count of DISTINCT record_ref among those issues (multiple
        issues, e.g. two failing rules, can point at the same source row)."""
        rows = self._db.execute(
            select(Issue.id, Issue.record_ref)
            .join(Correction, Correction.issue_id == Issue.id)
            .where(
                Issue.review_run_id == review_run_id,
                Correction.final_value.isnot(None),
                Correction.status.in_(_RESOLVED_CORRECTION_STATUSES),
            )
        ).all()
        return resolved_scope_from_rows(rows)

    def _decided_issue_ids(self, approval_request_id: uuid.UUID) -> set[uuid.UUID]:
        return set(
            self._db.execute(
                select(ApprovalDecisionIssue.issue_id)
                .join(ApprovalDecision, ApprovalDecision.id == ApprovalDecisionIssue.approval_decision_id)
                .where(ApprovalDecision.approval_request_id == approval_request_id)
            ).scalars()
        )

    def submit(self, review_run_id: uuid.UUID, actor: User) -> ApprovalRequest:
        review_run = self._db.get(ReviewRun, review_run_id)
        if review_run is None:
            raise ReviewRunNotFoundError(f"Review run {review_run_id} not found")
        if review_run.status != "IN_REVIEW":
            raise ReviewRunNotReadyForSubmissionError(
                f"Review run {review_run_id} is {review_run.status}, not IN_REVIEW"
            )

        existing = self._db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.review_run_id == review_run_id, ApprovalRequest.status.in_(_OPEN_APPROVAL_STATUSES)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ApprovalRequestAlreadyPendingError(
                f"Approval request {existing.id} is already {existing.status} for this review run"
            )

        issue_ids, record_count = self._resolved_scope(review_run_id)
        if not issue_ids:
            raise NoResolvedIssuesError(f"Review run {review_run_id} has zero resolved issues")

        approval_request = ApprovalRequest(
            review_run_id=review_run_id, status="PENDING",
            affected_issue_count=len(issue_ids), affected_record_count=record_count, requested_by=actor.id,
        )
        self._db.add(approval_request)
        self._db.flush()

        review_run.status = "READY_FOR_APPROVAL"
        review_run.updated_at = datetime.now(timezone.utc)

        # Phase 10 touch point 6 (additive-only): CORRECTION -> APPROVAL_REQUEST,
        # one per corrections row in the resolved-issue scope, written at
        # SUBMISSION time (regardless of eventual approve/reject outcome) as
        # ONE bulk insert, not a loop.
        correction_ids = self._db.execute(
            select(Correction.id).where(Correction.issue_id.in_(issue_ids))
        ).scalars().all()
        self._lineage.record_edges_bulk(
            [("CORRECTION", cid, "APPROVAL_REQUEST", approval_request.id, "APPROVED_BY") for cid in correction_ids]
        )

        self._audit.record(
            actor=actor, action="approval_request.submitted", entity_type="APPROVAL_REQUEST",
            entity_id=approval_request.id,
            metadata={
                "review_run_id": str(review_run_id), "affected_issue_count": len(issue_ids),
                "affected_record_count": record_count,
            },
        )
        self._db.commit()
        self._db.refresh(approval_request)
        return approval_request

    def decide(
        self, approval_request_id: uuid.UUID, *, decision: str, issue_ids: list[uuid.UUID],
        comment: str | None, actor: User,
    ) -> ApprovalRequest:
        # Locked design item 4 — mandatory row lock, not optional and not
        # satisfied by an application-level re-check alone. Serializes
        # concurrent decide() calls against the same approval_requests row.
        approval_request = self._db.execute(
            select(ApprovalRequest).where(ApprovalRequest.id == approval_request_id).with_for_update()
        ).scalar_one_or_none()
        if approval_request is None:
            raise ApprovalRequestNotFoundError(f"Approval request {approval_request_id} not found")

        resolved_issue_ids, _ = self._resolved_scope(approval_request.review_run_id)
        already_decided = self._decided_issue_ids(approval_request.id)
        remaining = set(resolved_issue_ids) - already_decided

        requested_ids = set(issue_ids)
        if not requested_ids or not requested_ids.issubset(remaining):
            invalid = requested_ids - remaining
            raise IssueNotInApprovalScopeError(
                f"Issue(s) not in the remaining undecided approval scope: {sorted(str(i) for i in invalid)}"
                if invalid
                else "issue_ids must be non-empty"
            )

        decision_row = ApprovalDecision(
            approval_request_id=approval_request.id, decision=decision, comment=comment, decided_by=actor.id
        )
        self._db.add(decision_row)
        self._db.flush()
        self._db.add_all(
            [ApprovalDecisionIssue(approval_decision_id=decision_row.id, issue_id=i) for i in requested_ids]
        )
        self._db.flush()

        new_remaining = remaining - requested_ids
        now = datetime.now(timezone.utc)
        all_decisions = list(
            self._db.execute(
                select(ApprovalDecision.decision).where(ApprovalDecision.approval_request_id == approval_request.id)
            ).scalars()
        )
        approval_request.status = recompute_approval_request_status(all_decisions, new_remaining)
        if not new_remaining:
            approval_request.decided_at = now
        approval_request.updated_at = now

        audit_metadata = {"decision": decision, "issue_count": len(requested_ids)}
        if comment:
            # User-authored free text — included here (the one authorized
            # location, per design) but never echoed anywhere else.
            audit_metadata["comment"] = comment
        self._audit.record(
            actor=actor, action="approval_request.decided", entity_type="APPROVAL_REQUEST",
            entity_id=approval_request.id, metadata=audit_metadata,
        )
        self._db.commit()
        self._db.refresh(approval_request)
        return approval_request

    def get_detail(self, approval_request_id: uuid.UUID) -> tuple[ApprovalRequest, int, int]:
        approval_request = self.get(approval_request_id)
        resolved_issue_ids, _ = self._resolved_scope(approval_request.review_run_id)
        decided_issue_ids = self._decided_issue_ids(approval_request.id)
        decided_count = len(decided_issue_ids)
        remaining_count = len(set(resolved_issue_ids) - decided_issue_ids)
        return approval_request, decided_count, remaining_count

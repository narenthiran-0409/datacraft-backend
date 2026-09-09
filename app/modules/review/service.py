import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    InvalidReviewStatusTransitionError,
    ReviewRunNotFoundError,
    SourceValidationRunIncompleteError,
    ValidationRunNotFoundError,
)
from app.db.models import ApprovalRequest, Issue, ReviewRun, User, ValidationFailure, ValidationResult, ValidationRun
from app.modules.audit.service import AuditingService
from app.modules.lineage.service import LineageService
from app.modules.review.state_machine import validate_archive_transition, validate_restore_transition


class ReviewService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)
        self._lineage = LineageService(db)

    def get(self, review_run_id: uuid.UUID) -> ReviewRun:
        review_run = self._db.get(ReviewRun, review_run_id)
        if review_run is None:
            raise ReviewRunNotFoundError(f"Review run {review_run_id} not found")
        return review_run

    def list_review_runs(self, *, validation_run_id: uuid.UUID | None, status: str | None) -> list[ReviewRun]:
        stmt = select(ReviewRun)
        if validation_run_id is not None:
            stmt = stmt.where(ReviewRun.validation_run_id == validation_run_id)
        if status is not None:
            stmt = stmt.where(ReviewRun.status == status)
        stmt = stmt.order_by(ReviewRun.created_at.desc())
        return list(self._db.execute(stmt).scalars())

    def list_issues(self, review_run_id: uuid.UUID, *, status: str | None) -> list[Issue]:
        self.get(review_run_id)
        stmt = select(Issue).where(Issue.review_run_id == review_run_id)
        if status is not None:
            stmt = stmt.where(Issue.status == status)
        stmt = stmt.order_by(Issue.row_index)
        return list(self._db.execute(stmt).scalars())

    def create_from_validation_run(
        self, *, validation_run_id: uuid.UUID, name: str | None, actor: User
    ) -> ReviewRun:
        validation_run = self._db.get(ValidationRun, validation_run_id)
        if validation_run is None:
            raise ValidationRunNotFoundError(f"Validation run {validation_run_id} not found")
        if validation_run.status != "COMPLETED":
            raise SourceValidationRunIncompleteError(
                f"Validation run {validation_run_id} is {validation_run.status}, not COMPLETED"
            )

        review_run = ReviewRun(
            validation_run_id=validation_run.id, name=name, status="DRAFT", created_by=actor.id
        )
        self._db.add(review_run)
        self._db.flush()

        # record_ref/row_index don't live on validation_failures — they're on
        # the validation_results row each failure belongs to. Joined here
        # rather than invented, since that's the only place they exist.
        failures_with_results = self._db.execute(
            select(ValidationFailure, ValidationResult)
            .join(ValidationResult, ValidationResult.id == ValidationFailure.validation_result_id)
            .where(ValidationFailure.validation_run_id == validation_run.id)
        ).all()

        issues = [
            Issue(
                review_run_id=review_run.id,
                validation_failure_id=vf.id,
                column_id=vf.column_id,
                record_ref=vr.record_ref,
                row_index=vr.row_index,
                original_value=vf.failed_value,
                severity=vf.severity,
                status="PENDING",
            )
            for vf, vr in failures_with_results
        ]
        self._db.add_all(issues)
        self._db.flush()

        # Phase 10 touch point 4 (additive-only): VALIDATION_RUN -> REVIEW_RUN,
        # and REVIEW_RUN -> ISSUE for every issue just bulk-inserted above, as
        # ONE bulk insert (not a loop) — same performance discipline as the
        # add_all() above.
        self._lineage.record_edge("VALIDATION_RUN", validation_run.id, "REVIEW_RUN", review_run.id, "DERIVED_FROM")
        self._lineage.record_edges_bulk(
            [("REVIEW_RUN", review_run.id, "ISSUE", issue.id, "DERIVED_FROM") for issue in issues]
        )

        self._audit.record(
            actor=actor,
            action="review_run.created",
            entity_type="REVIEW_RUN",
            entity_id=review_run.id,
            metadata={"validation_run_id": str(validation_run.id), "issue_count": len(issues)},
        )
        self._db.commit()
        self._db.refresh(review_run)
        return review_run

    def archive(self, review_run_id: uuid.UUID, actor: User) -> ReviewRun:
        review_run = self.get(review_run_id)
        validate_archive_transition(review_run.status)

        # Phase 7 archive guard (sanctioned, additive-only Phase 6 touch):
        # cannot archive a review run with an in-flight approval request.
        blocking_request = self._db.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.review_run_id == review_run.id,
                ApprovalRequest.status.in_(("PENDING", "PARTIALLY_APPROVED")),
            )
        ).scalar_one_or_none()
        if blocking_request is not None:
            raise InvalidReviewStatusTransitionError(
                f"Cannot archive review run {review_run_id}: approval request {blocking_request.id} is "
                f"{blocking_request.status}"
            )

        now = datetime.now(timezone.utc)
        review_run.status = "ARCHIVED"
        review_run.archived_at = now
        review_run.updated_at = now

        self._audit.record(
            actor=actor, action="review_run.archived", entity_type="REVIEW_RUN", entity_id=review_run.id
        )
        self._db.commit()
        self._db.refresh(review_run)
        return review_run

    def restore(self, review_run_id: uuid.UUID, actor: User) -> ReviewRun:
        review_run = self.get(review_run_id)
        validate_restore_transition(review_run.status)

        now = datetime.now(timezone.utc)
        review_run.status = "IN_REVIEW"
        review_run.archived_at = None
        review_run.updated_at = now

        self._audit.record(
            actor=actor, action="review_run.restored", entity_type="REVIEW_RUN", entity_id=review_run.id
        )
        self._db.commit()
        self._db.refresh(review_run)
        return review_run

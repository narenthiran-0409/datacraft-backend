import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    EmptyFinalValueError,
    InvalidReviewStatusTransitionError,
    IssueNotFoundError,
    ReviewRunNotFoundError,
    SuggestionAlreadyDecidedError,
    SuggestionNotFoundError,
)
from app.db.models import (
    Correction,
    CorrectionSuggestion,
    Issue,
    ReviewRun,
    Rule,
    RuleAssignment,
    RuleVersion,
    User,
    ValidationFailure,
)
from app.modules.audit.service import AuditingService
from app.modules.lineage.service import LineageService

_TERMINAL_STATUSES = frozenset({"REJECTED", "SKIPPED"})


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


class CorrectionDecisionService:
    """The ONLY code path permitted to write corrections.final_value/status.

    Audit metadata for every action here is strictly structural (issue_id,
    column_id, rule_type, decision/action type, summary counts) — never the
    actual suggested_value/final_value content. This is verified in
    integration tests by inspecting persisted audit_events rows directly."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)
        self._lineage = LineageService(db)

    def _get_suggestion(self, suggestion_id: uuid.UUID) -> CorrectionSuggestion:
        suggestion = self._db.get(CorrectionSuggestion, suggestion_id)
        if suggestion is None:
            raise SuggestionNotFoundError(f"Suggestion {suggestion_id} not found")
        return suggestion

    def _get_issue(self, issue_id: uuid.UUID) -> Issue:
        issue = self._db.get(Issue, issue_id)
        if issue is None:
            raise IssueNotFoundError(f"Issue {issue_id} not found")
        return issue

    def _existing_correction(self, issue_id: uuid.UUID) -> Correction | None:
        return self._db.execute(select(Correction).where(Correction.issue_id == issue_id)).scalar_one_or_none()

    def _check_not_terminal(self, correction: Correction | None) -> None:
        if correction is not None and correction.status in _TERMINAL_STATUSES:
            raise SuggestionAlreadyDecidedError(
                f"Issue already has a terminal decision ({correction.status}); no further action is accepted"
            )

    def _check_review_run_not_ready_for_approval(self, review_run_id: uuid.UUID) -> None:
        """Phase 7 correction-decision guard (sanctioned, additive-only
        Phase 6 touch): once a review run's status is READY_FOR_APPROVAL,
        no further correction decisions are accepted — the resolved-issue
        scope must stay stable once an approval request has been
        submitted against it."""
        review_run = self._db.get(ReviewRun, review_run_id)
        if review_run is not None and review_run.status == "READY_FOR_APPROVAL":
            raise InvalidReviewStatusTransitionError(
                f"Review run {review_run_id} is READY_FOR_APPROVAL; no further correction decisions are accepted"
            )

    def _resolve_rule_type(self, issue: Issue) -> str:
        validation_failure = self._db.get(ValidationFailure, issue.validation_failure_id)
        rule_assignment = self._db.get(RuleAssignment, validation_failure.rule_assignment_id)
        rule_version = self._db.get(RuleVersion, rule_assignment.rule_version_id)
        rule = self._db.get(Rule, rule_version.rule_id)
        return rule.rule_type

    def _audit_metadata(self, issue: Issue, decision: str) -> dict:
        return {
            "issue_id": str(issue.id),
            "column_id": str(issue.column_id) if issue.column_id else None,
            "rule_type": self._resolve_rule_type(issue),
            "decision": decision,
        }

    def _upsert_correction(
        self, *, issue: Issue, correction_suggestion_id: uuid.UUID | None, final_value: str | None,
        value_source: str | None, status: str, actor: User,
    ) -> Correction:
        correction = self._existing_correction(issue.id)
        now = datetime.now(timezone.utc)
        if correction is None:
            correction = Correction(
                issue_id=issue.id, correction_suggestion_id=correction_suggestion_id, final_value=final_value,
                value_source=value_source, status=status, decided_by=actor.id, decided_at=now,
            )
            self._db.add(correction)
            self._db.flush()
            # Phase 10 touch point 5 (additive-only): ISSUE -> CORRECTION,
            # written ONLY at first creation (this branch) — subsequent
            # re-decisions update this same row (the else branch below),
            # so the edge's child_entity_id never changes and is never
            # re-written on re-decision.
            self._lineage.record_edge("ISSUE", issue.id, "CORRECTION", correction.id, "CORRECTED_BY")
        else:
            correction.correction_suggestion_id = correction_suggestion_id
            correction.final_value = final_value
            correction.value_source = value_source
            correction.status = status
            correction.decided_by = actor.id
            correction.decided_at = now
            correction.updated_at = now
        self._db.flush()
        return correction

    def _clear_other_selections(self, issue_id: uuid.UUID, keep_suggestion_id: uuid.UUID) -> None:
        others = self._db.execute(
            select(CorrectionSuggestion).where(
                CorrectionSuggestion.issue_id == issue_id, CorrectionSuggestion.id != keep_suggestion_id
            )
        ).scalars()
        for other in others:
            other.is_selected = False

    def accept(self, suggestion_id: uuid.UUID, actor: User) -> Correction:
        suggestion = self._get_suggestion(suggestion_id)
        issue = self._get_issue(suggestion.issue_id)
        self._check_review_run_not_ready_for_approval(issue.review_run_id)
        self._check_not_terminal(self._existing_correction(issue.id))

        if _is_blank(suggestion.suggested_value):
            raise EmptyFinalValueError("Suggestion has no usable value")

        correction = self._upsert_correction(
            issue=issue, correction_suggestion_id=suggestion.id, final_value=suggestion.suggested_value,
            value_source=suggestion.source, status="ACCEPTED", actor=actor,
        )
        self._clear_other_selections(issue.id, suggestion.id)
        suggestion.is_selected = True
        suggestion.selected_by = actor.id
        suggestion.selected_at = datetime.now(timezone.utc)

        issue.status = "RESOLVED"
        issue.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor, action="issue.accepted", entity_type="ISSUE", entity_id=issue.id,
            metadata=self._audit_metadata(issue, "ACCEPTED"),
        )
        self._db.commit()
        self._db.refresh(correction)
        return correction

    def edit(self, suggestion_id: uuid.UUID, final_value: str, actor: User) -> Correction:
        suggestion = self._get_suggestion(suggestion_id)
        issue = self._get_issue(suggestion.issue_id)
        self._check_review_run_not_ready_for_approval(issue.review_run_id)
        self._check_not_terminal(self._existing_correction(issue.id))

        if _is_blank(final_value):
            raise EmptyFinalValueError("final_value must be non-blank")

        correction = self._upsert_correction(
            issue=issue, correction_suggestion_id=suggestion.id, final_value=final_value,
            value_source="HUMAN", status="EDITED", actor=actor,
        )
        self._clear_other_selections(issue.id, suggestion.id)
        suggestion.is_selected = True
        suggestion.selected_by = actor.id
        suggestion.selected_at = datetime.now(timezone.utc)

        issue.status = "RESOLVED"
        issue.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor, action="issue.edited", entity_type="ISSUE", entity_id=issue.id,
            metadata=self._audit_metadata(issue, "EDITED"),
        )
        self._db.commit()
        self._db.refresh(correction)
        return correction

    def reject(self, suggestion_id: uuid.UUID, reason: str | None, actor: User) -> Correction:
        suggestion = self._get_suggestion(suggestion_id)
        issue = self._get_issue(suggestion.issue_id)
        self._check_review_run_not_ready_for_approval(issue.review_run_id)
        self._check_not_terminal(self._existing_correction(issue.id))

        correction = self._upsert_correction(
            issue=issue, correction_suggestion_id=suggestion.id, final_value=None,
            value_source=suggestion.source, status="REJECTED", actor=actor,
        )

        issue.status = "RESOLVED"
        issue.updated_at = datetime.now(timezone.utc)

        metadata = self._audit_metadata(issue, "REJECTED")
        if reason:
            metadata["reason"] = reason  # a rejection justification, not a raw suggested/final value
        self._audit.record(actor=actor, action="issue.rejected", entity_type="ISSUE", entity_id=issue.id, metadata=metadata)
        self._db.commit()
        self._db.refresh(correction)
        return correction

    def skip(self, issue_id: uuid.UUID, actor: User) -> Correction:
        issue = self._get_issue(issue_id)
        self._check_review_run_not_ready_for_approval(issue.review_run_id)
        self._check_not_terminal(self._existing_correction(issue.id))

        correction = self._upsert_correction(
            issue=issue, correction_suggestion_id=None, final_value=None, value_source=None,
            status="SKIPPED", actor=actor,
        )

        issue.status = "SKIPPED"
        issue.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor, action="issue.skipped", entity_type="ISSUE", entity_id=issue.id,
            metadata=self._audit_metadata(issue, "SKIPPED"),
        )
        self._db.commit()
        self._db.refresh(correction)
        return correction

    def correct_directly(self, issue_id: uuid.UUID, final_value: str, actor: User) -> Correction:
        issue = self._get_issue(issue_id)
        self._check_review_run_not_ready_for_approval(issue.review_run_id)
        self._check_not_terminal(self._existing_correction(issue.id))

        if _is_blank(final_value):
            raise EmptyFinalValueError("final_value must be non-blank")

        correction = self._upsert_correction(
            issue=issue, correction_suggestion_id=None, final_value=final_value,
            value_source="HUMAN", status="EDITED", actor=actor,
        )

        issue.status = "RESOLVED"
        issue.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor, action="issue.corrected_directly", entity_type="ISSUE", entity_id=issue.id,
            metadata=self._audit_metadata(issue, "CORRECTED_DIRECTLY"),
        )
        self._db.commit()
        self._db.refresh(correction)
        return correction

    def bulk_action(self, *, review_run_id: uuid.UUID, issue_ids: list[uuid.UUID], action: str, actor: User) -> dict:
        """One transaction for the whole call: every issue_id is validated
        (found, not already terminal) BEFORE any mutation happens, so the
        call is atomic — either all listed issues transition together, or
        none do (the first invalid issue raises before anything is
        committed). Only "skip" and "reject" are supported in bulk — accept/
        edit inherently require picking a specific value per issue, and no
        auto-selection heuristic was requested or implemented."""
        review_run = self._db.get(ReviewRun, review_run_id)
        if review_run is None:
            raise ReviewRunNotFoundError(f"Review run {review_run_id} not found")
        if review_run.status == "READY_FOR_APPROVAL":
            raise InvalidReviewStatusTransitionError(
                f"Review run {review_run_id} is READY_FOR_APPROVAL; no further correction decisions are accepted"
            )

        issues = []
        for issue_id in issue_ids:
            issue = self._get_issue(issue_id)
            self._check_not_terminal(self._existing_correction(issue.id))
            issues.append(issue)

        for issue in issues:
            if action == "skip":
                self._upsert_correction(
                    issue=issue, correction_suggestion_id=None, final_value=None, value_source=None,
                    status="SKIPPED", actor=actor,
                )
                issue.status = "SKIPPED"
            else:  # "reject" — validated at the API schema layer (Literal["skip","reject"])
                self._upsert_correction(
                    issue=issue, correction_suggestion_id=None, final_value=None, value_source=None,
                    status="REJECTED", actor=actor,
                )
                issue.status = "RESOLVED"
            issue.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor, action="issue.bulk_action", entity_type="REVIEW_RUN", entity_id=review_run.id,
            metadata={"action": action, "issue_count": len(issues)},
        )
        self._db.commit()
        return {"action": action, "issue_count": len(issues)}

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    Column,
    ColumnProfile,
    CorrectionSuggestion,
    Issue,
    ProfileRun,
    ReviewRun,
    Rule,
    RuleAssignment,
    RuleVersion,
    User,
    ValidationFailure,
    ValidationRun,
)
from app.core.exceptions import ReviewRunNotFoundError
from app.modules.audit.service import AuditingService
from app.modules.review.generators import CORRECTION_GENERATOR_REGISTRY, GeneratorContext

_BATCH_SIZE = 200


class SuggestionService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)
        self._column_profile_cache: dict[uuid.UUID, ColumnProfile | None] = {}
        self._rule_chain_cache: dict[uuid.UUID, tuple[RuleVersion, Rule]] = {}

    def _latest_column_profile(self, dataset_id: uuid.UUID, column_id: uuid.UUID) -> ColumnProfile | None:
        if column_id in self._column_profile_cache:
            return self._column_profile_cache[column_id]

        latest_run_id = self._db.execute(
            select(ProfileRun.id)
            .where(ProfileRun.dataset_id == dataset_id, ProfileRun.status == "COMPLETED")
            .order_by(ProfileRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        profile = None
        if latest_run_id is not None:
            profile = self._db.execute(
                select(ColumnProfile).where(
                    ColumnProfile.profile_run_id == latest_run_id, ColumnProfile.column_id == column_id
                )
            ).scalar_one_or_none()

        self._column_profile_cache[column_id] = profile
        return profile

    def _rule_chain(self, rule_assignment_id: uuid.UUID) -> tuple[RuleVersion, Rule]:
        if rule_assignment_id in self._rule_chain_cache:
            return self._rule_chain_cache[rule_assignment_id]

        rule_assignment = self._db.get(RuleAssignment, rule_assignment_id)
        rule_version = self._db.get(RuleVersion, rule_assignment.rule_version_id)
        rule = self._db.get(Rule, rule_version.rule_id)

        self._rule_chain_cache[rule_assignment_id] = (rule_version, rule)
        return rule_version, rule

    def generate_for_review_run(self, review_run_id: uuid.UUID, actor: User) -> dict:
        review_run = self._db.get(ReviewRun, review_run_id)
        if review_run is None:
            raise ReviewRunNotFoundError(f"Review run {review_run_id} not found")

        # Phase 7 fix (see MANDATORY INSPECTION finding in the Phase 7 report):
        # this was the original design's intended DRAFT -> IN_REVIEW trigger
        # ("DRAFT -> IN_REVIEW via save_draft/generate_suggestions") and was
        # never implemented in Phase 6. Guarded so calling generate-suggestions
        # again later (already IN_REVIEW or beyond) is a no-op here.
        if review_run.status == "DRAFT":
            review_run.status = "IN_REVIEW"

        validation_run = self._db.get(ValidationRun, review_run.validation_run_id)
        dataset_id = validation_run.dataset_id

        pending_issues = list(
            self._db.execute(
                select(Issue).where(Issue.review_run_id == review_run_id, Issue.status == "PENDING")
            ).scalars()
        )
        if not pending_issues:
            self._audit.record(
                actor=actor, action="suggestions.generated", entity_type="REVIEW_RUN", entity_id=review_run.id,
                metadata={"generated_count": 0, "issues_with_no_suggestion_count": 0},
            )
            self._db.commit()
            return {"generated_count": 0, "issues_with_no_suggestion_count": 0}

        issue_ids = [i.id for i in pending_issues]
        issues_with_suggestions = set(
            self._db.execute(
                select(CorrectionSuggestion.issue_id)
                .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
                .where(Issue.id.in_(issue_ids))
            ).scalars()
        )
        target_issues = [i for i in pending_issues if i.id not in issues_with_suggestions]

        generated_count = 0
        no_suggestion_count = 0

        for batch_start in range(0, len(target_issues), _BATCH_SIZE):
            batch = target_issues[batch_start : batch_start + _BATCH_SIZE]
            for issue in batch:
                validation_failure = self._db.get(ValidationFailure, issue.validation_failure_id)
                rule_version, rule = self._rule_chain(validation_failure.rule_assignment_id)
                column = self._db.get(Column, issue.column_id) if issue.column_id else None
                column_profile = (
                    self._latest_column_profile(dataset_id, issue.column_id) if issue.column_id else None
                )

                ctx = GeneratorContext(
                    issue=issue, validation_failure=validation_failure, column=column,
                    column_profile=column_profile, rule_version=rule_version,
                )

                generator_classes = CORRECTION_GENERATOR_REGISTRY.get(rule.rule_type, [])
                any_suggestion = False
                for generator_cls in generator_classes:
                    draft = generator_cls().generate(ctx)
                    if draft is not None:
                        self._db.add(
                            CorrectionSuggestion(
                                issue_id=issue.id, source="RULE_BASED", suggested_value=draft.suggested_value,
                                confidence=draft.confidence, fix_type=generator_cls.fix_type,
                                reasoning=draft.reasoning, is_selected=False,
                            )
                        )
                        generated_count += 1
                        any_suggestion = True

                if not any_suggestion:
                    no_suggestion_count += 1

            self._db.commit()  # one commit per batch, not one giant transaction across the run

        self._audit.record(
            actor=actor, action="suggestions.generated", entity_type="REVIEW_RUN", entity_id=review_run.id,
            metadata={"generated_count": generated_count, "issues_with_no_suggestion_count": no_suggestion_count},
        )
        self._db.commit()
        return {"generated_count": generated_count, "issues_with_no_suggestion_count": no_suggestion_count}

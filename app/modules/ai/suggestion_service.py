import json
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AIResponseInvalidError, AISuggestionNotFoundError, IssueNotFoundError
from app.db.models import (
    AISuggestion,
    Column,
    ColumnProfile,
    CorrectionSuggestion,
    Dataset,
    Issue,
    ReviewRun,
    Rule,
    RuleAssignment,
    RuleVersion,
    User,
    ValidationFailure,
    ValidationRun,
)
from app.modules.ai.context import (
    build_issue_context,
    build_review_run_context,
    build_rule_recommendation_context,
    build_run_summary_context,
)
from app.modules.ai.orchestrator_service import AIOrchestratorService

_PROMPT_KEY_EXPLANATION = "ai_explanation"
_PROMPT_KEY_RUN_SUMMARY = "ai_run_summary"
_PROMPT_KEY_PRIORITIZATION = "ai_prioritization"
_PROMPT_KEY_CLUSTER = "ai_cluster"
_PROMPT_KEY_CORRECTION = "ai_correction"
_PROMPT_KEY_RULE_RECOMMENDATION = "ai_rule_recommendation"

# correction_suggestions.confidence is NOT NULL (frozen Phase 6 schema) —
# this simple text-response design doesn't extract a structured confidence
# score from the provider's free-text output, so a neutral, explicitly
# documented placeholder is used rather than fabricating false precision.
# ai_suggestions.confidence (nullable) correctly stays unset/None instead.
_UNSTRUCTURED_RESPONSE_PLACEHOLDER_CONFIDENCE = Decimal("0.500")


class AISuggestionService:
    """Every ai_suggestions row this service creates always carries full
    provenance (provider, model, prompt_version_id, requested_by,
    created_at, response_metadata with the input context hash, confidence)
    and starts at status=PROPOSED — AI output never directly mutates any
    authoritative business table.

    For suggestion_type="CORRECTION", this service ALSO inserts a
    correction_suggestions row (source="AI", ai_suggestion_id=<the new
    ai_suggestions id>) using the existing Phase 6 model/table AS-IS. This
    plain INSERT is the ONLY write this entire implementation ever makes
    into any Phase 6 table — SuggestionService.generate_for_review_run(),
    CORRECTION_GENERATOR_REGISTRY, and CorrectionDecisionService are never
    called, never imported for mutation, never modified."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._orchestrator = AIOrchestratorService(db)

    def get(self, suggestion_id: uuid.UUID) -> AISuggestion:
        suggestion = self._db.get(AISuggestion, suggestion_id)
        if suggestion is None:
            raise AISuggestionNotFoundError(f"AI suggestion {suggestion_id} not found")
        return suggestion

    def _get_issue(self, issue_id: uuid.UUID) -> Issue:
        issue = self._db.get(Issue, issue_id)
        if issue is None:
            raise IssueNotFoundError(f"Issue {issue_id} not found")
        return issue

    def _issue_context(self, issue: Issue) -> dict:
        validation_failure = self._db.get(ValidationFailure, issue.validation_failure_id)
        rule_assignment = self._db.get(RuleAssignment, validation_failure.rule_assignment_id)
        rule_version = self._db.get(RuleVersion, rule_assignment.rule_version_id)
        rule = self._db.get(Rule, rule_version.rule_id)
        column = self._db.get(Column, issue.column_id) if issue.column_id else None
        return build_issue_context(
            issue=issue, column=column, validation_failure=validation_failure, rule=rule, rule_version=rule_version
        )

    def _create_suggestion(
        self, *, suggestion_type: str, source_context_type: str, source_context_id: uuid.UUID,
        content: dict, provider: str, model: str, prompt_version_id: uuid.UUID, conversation_id: uuid.UUID | None,
        actor: User, context_hash: str,
    ) -> AISuggestion:
        suggestion = AISuggestion(
            suggestion_type=suggestion_type, source_context_type=source_context_type,
            source_context_id=source_context_id, content=content, provider=provider, model=model,
            prompt_version_id=prompt_version_id, conversation_id=conversation_id, requested_by=actor.id,
            status="PROPOSED", response_metadata={"input_context_hash": context_hash},
        )
        self._db.add(suggestion)
        self._db.flush()
        return suggestion

    # --- EXPLANATION (synchronous) ------------------------------------------

    def generate_explanation(self, issue_id: uuid.UUID, actor: User) -> AISuggestion:
        issue = self._get_issue(issue_id)
        context = self._issue_context(issue)

        result = self._orchestrator.run(prompt_key=_PROMPT_KEY_EXPLANATION, context=context, actor=actor)
        suggestion = self._create_suggestion(
            suggestion_type="EXPLANATION", source_context_type="ISSUE", source_context_id=issue.id,
            content={"text": result.text}, provider=result.provider, model=result.model,
            prompt_version_id=result.prompt_version.id, conversation_id=None, actor=actor,
            context_hash=result.context_hash,
        )
        self._db.commit()
        self._db.refresh(suggestion)
        return suggestion

    # --- RUN_SUMMARY (async, called from Celery task) -----------------------

    def generate_run_summary(self, validation_run_id: uuid.UUID, actor: User) -> AISuggestion:
        validation_run = self._db.get(ValidationRun, validation_run_id)
        dataset = self._db.get(Dataset, validation_run.dataset_id)

        failure_rows = self._db.execute(
            select(ValidationFailure.severity)
            .where(ValidationFailure.validation_run_id == validation_run.id)
        ).scalars().all()
        failure_counts_by_severity: dict[str, int] = {}
        for severity in failure_rows:
            failure_counts_by_severity[severity] = failure_counts_by_severity.get(severity, 0) + 1

        context = build_run_summary_context(
            validation_run=validation_run, dataset=dataset, failure_counts_by_severity=failure_counts_by_severity
        )
        result = self._orchestrator.run(prompt_key=_PROMPT_KEY_RUN_SUMMARY, context=context, actor=actor)
        suggestion = self._create_suggestion(
            suggestion_type="RUN_SUMMARY", source_context_type="VALIDATION_RUN", source_context_id=validation_run.id,
            content={"text": result.text}, provider=result.provider, model=result.model,
            prompt_version_id=result.prompt_version.id, conversation_id=None, actor=actor,
            context_hash=result.context_hash,
        )
        self._db.commit()
        self._db.refresh(suggestion)
        return suggestion

    # --- PRIORITIZATION / CLUSTER (async, called from Celery task) ---------

    def _review_run_issue_summaries(self, review_run_id: uuid.UUID) -> list[dict]:
        issues = self._db.execute(select(Issue).where(Issue.review_run_id == review_run_id)).scalars().all()
        summaries = []
        for issue in issues:
            column = self._db.get(Column, issue.column_id) if issue.column_id else None
            summaries.append(
                {
                    "issue_id": str(issue.id), "severity": issue.severity, "status": issue.status,
                    "column_name": column.name if column is not None else None,
                }
            )
        return summaries

    def generate_prioritization(self, review_run_id: uuid.UUID, actor: User) -> AISuggestion:
        review_run = self._db.get(ReviewRun, review_run_id)
        context = build_review_run_context(
            review_run=review_run, issue_summaries=self._review_run_issue_summaries(review_run_id)
        )
        result = self._orchestrator.run(prompt_key=_PROMPT_KEY_PRIORITIZATION, context=context, actor=actor)
        suggestion = self._create_suggestion(
            suggestion_type="PRIORITIZATION", source_context_type="REVIEW_RUN", source_context_id=review_run.id,
            content={"text": result.text}, provider=result.provider, model=result.model,
            prompt_version_id=result.prompt_version.id, conversation_id=None, actor=actor,
            context_hash=result.context_hash,
        )
        self._db.commit()
        self._db.refresh(suggestion)
        return suggestion

    def generate_cluster(self, review_run_id: uuid.UUID, actor: User) -> AISuggestion:
        review_run = self._db.get(ReviewRun, review_run_id)
        context = build_review_run_context(
            review_run=review_run, issue_summaries=self._review_run_issue_summaries(review_run_id)
        )
        result = self._orchestrator.run(prompt_key=_PROMPT_KEY_CLUSTER, context=context, actor=actor)
        suggestion = self._create_suggestion(
            suggestion_type="CLUSTER", source_context_type="REVIEW_RUN", source_context_id=review_run.id,
            content={"text": result.text}, provider=result.provider, model=result.model,
            prompt_version_id=result.prompt_version.id, conversation_id=None, actor=actor,
            context_hash=result.context_hash,
        )
        self._db.commit()
        self._db.refresh(suggestion)
        return suggestion

    # --- CORRECTION (async, called from Celery task) — the Phase 6 bridge --

    def generate_corrections(self, review_run_id: uuid.UUID, actor: User) -> list[AISuggestion]:
        """One ai_suggestions row PER issue, each immediately bridged into
        a correction_suggestions row (source="AI", ai_suggestion_id=<the
        new id>) — a plain INSERT using the existing, unmodified Phase 6
        model. Only issues without an existing correction_suggestions row
        are considered, mirroring generate_for_review_run()'s own
        already-suggested exclusion (read-only inspection of that
        existing table, never a call into that service)."""
        issues = self._db.execute(
            select(Issue).where(Issue.review_run_id == review_run_id, Issue.status == "PENDING")
        ).scalars().all()
        issue_ids = [i.id for i in issues]
        already_suggested = set(
            self._db.execute(
                select(CorrectionSuggestion.issue_id).where(CorrectionSuggestion.issue_id.in_(issue_ids))
            ).scalars()
        ) if issue_ids else set()
        target_issues = [i for i in issues if i.id not in already_suggested]

        created: list[AISuggestion] = []
        for issue in target_issues:
            context = self._issue_context(issue)
            result = self._orchestrator.run(prompt_key=_PROMPT_KEY_CORRECTION, context=context, actor=actor)

            suggestion = self._create_suggestion(
                suggestion_type="CORRECTION", source_context_type="ISSUE", source_context_id=issue.id,
                content={"text": result.text}, provider=result.provider, model=result.model,
                prompt_version_id=result.prompt_version.id, conversation_id=None, actor=actor,
                context_hash=result.context_hash,
            )

            # The one and only write into any Phase 6 table anywhere in
            # this implementation — a plain INSERT, no call into
            # SuggestionService/CorrectionDecisionService.
            self._db.add(
                CorrectionSuggestion(
                    issue_id=issue.id, source="AI", ai_suggestion_id=suggestion.id,
                    suggested_value=result.text, confidence=_UNSTRUCTURED_RESPONSE_PLACEHOLDER_CONFIDENCE,
                    fix_type="AI_PROPOSED", reasoning=None, is_selected=False,
                )
            )
            self._db.commit()
            self._db.refresh(suggestion)
            created.append(suggestion)

        return created

    # --- RULE_RECOMMENDATION (synchronous fallback, called from RuleDetectionService) --

    def generate_rule_recommendations(
        self, *, dataset: Dataset, columns_with_profiles: list[tuple[Column, ColumnProfile | None]], actor: User,
    ) -> tuple[AISuggestion, list[dict]]:
        """One LLM call, one ai_suggestions row, covering every column
        passed in — never one call per column (RuleDetectionService is
        responsible for only passing the columns its pattern-matching half
        wasn't confident about, and for capping how many go through this
        path). Returns the created suggestion alongside the parsed
        recommendations so the caller can decide what to do with them —
        this method never creates or touches a `rules` row itself; that
        stays RuleDetectionService's job, going through RulesService.
        create_rule() so the PENDING_REVIEW guarantee is enforced in
        exactly one place regardless of which half of the detector
        produced the candidate."""
        context = build_rule_recommendation_context(dataset=dataset, columns_with_profiles=columns_with_profiles)
        result = self._orchestrator.run(prompt_key=_PROMPT_KEY_RULE_RECOMMENDATION, context=context, actor=actor)

        recommendations = _parse_rule_recommendations(result.text)

        suggestion = self._create_suggestion(
            suggestion_type="RULE_RECOMMENDATION", source_context_type="DATASET", source_context_id=dataset.id,
            content={"text": result.text, "parsed_count": len(recommendations)},
            provider=result.provider, model=result.model, prompt_version_id=result.prompt_version.id,
            conversation_id=None, actor=actor, context_hash=result.context_hash,
        )
        self._db.commit()
        self._db.refresh(suggestion)
        return suggestion, recommendations


def _parse_rule_recommendations(text: str) -> list[dict]:
    """Defensive parse of the LLM's JSON response — strips a markdown code
    fence if present (models commonly wrap JSON in ```json ... ``` even
    when told not to), then requires a JSON array. Returns [] (not an
    error) for a syntactically valid empty array — "no column here
    warrants a rule" is a legitimate answer, not a failure. Raises
    AIResponseInvalidError only when the response can't be interpreted as
    the requested shape at all, since nothing downstream can safely act
    on an unparseable proposal — matches this project's existing
    AIResponseInvalidError semantics (HTTP 502, not swallowed silently)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseInvalidError(f"Rule recommendation response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise AIResponseInvalidError("Rule recommendation response was not a JSON array")
    return [item for item in parsed if isinstance(item, dict)]

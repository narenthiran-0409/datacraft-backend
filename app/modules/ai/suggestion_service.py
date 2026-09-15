import json
import math
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AIResponseInvalidError, AISuggestionNotFoundError, IssueNotFoundError
from app.core.redis_client import get_redis_client
from app.db.models import (
    AISuggestion,
    AIUsageLog,
    AuditEvent,
    Column,
    ColumnProfile,
    Connection,
    ConnectionType,
    CorrectionSuggestion,
    Dataset,
    DatasetKeyColumn,
    Issue,
    ProfileRun,
    ReviewRun,
    Rule,
    RuleAssignment,
    RuleVersion,
    Schema,
    User,
    ValidationFailure,
    ValidationResult,
    ValidationRun,
)
from app.modules.ai.context import (
    build_correction_context,
    build_issue_context,
    build_review_run_context,
    build_rule_recommendation_context,
    build_run_summary_context,
)
from app.modules.ai.candidates import AggregationResult, Candidate, aggregate_candidates
from app.modules.ai.evidence import ColumnMeta as _EvidenceColumnMeta
from app.modules.ai.evidence import EvidenceResult, discover_relationship_evidence
from app.modules.ai.evidence_sequence import discover_sequence_evidence
from app.modules.ai.evidence_template import discover_template_evidence
from app.modules.ai.evidence_temporal import discover_temporal_evidence
from app.modules.ai.orchestrator_service import AIOrchestratorService
from app.modules.ai.prompt_service import PromptVersionService
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.staging.record_builder import parse_record_ref_to_key_dict
from app.modules.validation.record_ref import generate_record_ref
from app.source_adapters.factory import get_provider

_PROMPT_KEY_EXPLANATION = "ai_explanation"
_PROMPT_KEY_RUN_SUMMARY = "ai_run_summary"
_PROMPT_KEY_PRIORITIZATION = "ai_prioritization"
_PROMPT_KEY_CLUSTER = "ai_cluster"
_PROMPT_KEY_CORRECTION = "ai_correction"
_PROMPT_KEY_RULE_RECOMMENDATION = "ai_rule_recommendation"

# The AI path never claims DETERMINISTIC — that category is reserved for the
# rule-based generator registry (app.modules.review.generators), which is
# always tried first and always wins when it produces a result.
_AI_CORRECTION_CATEGORIES = frozenset({"AI_HIGH_CONFIDENCE", "NEEDS_REVIEW", "CANNOT_INFER"})
_DUPLICATE_EXAMPLE_LIMIT = 5
_UNIQUENESS_LIKE_RULE_TYPES = frozenset({"UNIQUENESS", "DUPLICATE"})

# --- Phase 4.5: engine applicability -----------------------------------------
# Deliberately based ONLY on column.normalized_data_type + the violated
# rule's rule_type — both are already-computed metadata, never a column
# NAME or any business-specific assumption. rule_type is one of the six
# values in app.modules.rules.service.SUPPORTED_RULE_TYPES — the
# authoritative, only-existing enum in this repository (COMPLETENESS,
# UNIQUENESS, DUPLICATE, RANGE, PATTERN, CROSS_COLUMN); nothing here
# invents a new one.
#
# A "gap-fill" issue (COMPLETENESS: the target is null/missing) and a
# "resolve this duplicate" issue (UNIQUENESS/DUPLICATE: the target is a
# repeated value) are the only two situations SEQUENCE_GAP/
# SEQUENCE_NEXT_VALUE and TEMPORAL_GAP/TEMPORAL_NEXT_VALUE evidence are
# ever meaningful for — a RANGE or PATTERN violation on the very same
# column is a "this specific value looks wrong" problem, not a "this
# value is missing/duplicated" problem, so sequence/temporal evidence is
# not attempted for those.
_SEQUENCE_APPLICABLE_TYPES = frozenset({"INTEGER", "STRING"})
_TEMPORAL_APPLICABLE_TYPES = frozenset({"DATE", "DATETIME"})
_TEMPLATE_APPLICABLE_TYPES = frozenset({"STRING", "TEXT"})
_GAP_LIKE_RULE_TYPES = frozenset({"COMPLETENESS"})
_NEXT_VALUE_LIKE_RULE_TYPES = frozenset({"UNIQUENESS", "DUPLICATE"})
_SEQUENCE_PROGRESSION_RULE_TYPES = _GAP_LIKE_RULE_TYPES | _NEXT_VALUE_LIKE_RULE_TYPES
# TEMPLATE evidence answers "what should this string be", which is
# meaningful for a missing value (COMPLETENESS) or a malformed one
# (PATTERN) — never for a duplicate (there is no "other column" template
# that resolves which of two identical values is authoritative).
_TEMPLATE_APPLICABLE_RULE_TYPES = frozenset({"COMPLETENESS", "PATTERN"})

# Phase 4.5 acceptance fix: RELATIONSHIP evidence is a cross-column
# "does this value make sense relative to OTHER columns" check — genuinely
# meaningful for RANGE (an out-of-bounds/implausible value) and
# CROSS_COLUMN (literally a cross-column rule); PATTERN is included too
# for the rare case of a numeric column with a format rule. It is
# deliberately NEVER attempted for COMPLETENESS/UNIQUENESS/DUPLICATE on a
# numeric column, even though normalized_data_type alone (INTEGER/DECIMAL)
# would otherwise qualify: those three rule types are exactly the ones
# SEQUENCE/TEMPORAL exist to handle via progression evidence, and
# app.modules.ai.evidence's own constant_within_group shape (Phase 0,
# unmodified) can spuriously "fit" a tightly-clustered numeric identifier
# column (e.g. 1001..1008) purely because coefficient-of-variation is
# small relative to a large mean — a real property of that column's
# VALUES, not a name-based or rule-type-based special case being carved
# out here. Gating RELATIONSHIP off for these three rule types prevents
# it from ever competing with the progression strategies that are the
# actually-correct evidence for a gap/duplicate issue, without touching
# evidence.py itself and without special-casing any column name or value.
# A genuine "this numeric value can be derived from another column, AND
# it's also missing" scenario (e.g. Salary missing but Tax=0.2*Salary
# known) is consequently not handled by RELATIONSHIP in this phase for a
# COMPLETENESS issue — a documented, conservative limitation, not a bug;
# SEQUENCE still applies if the column's own values form a progression.
_RELATIONSHIP_APPLICABLE_RULE_TYPES = frozenset({"RANGE", "PATTERN", "CROSS_COLUMN"})


def _applicable_strategies(column: Column, rule_type: str) -> frozenset[str]:
    """Which of the four Phase 4 evidence engines are even worth running
    for this column/rule_type combination. Never reads column.name."""
    applicable: set[str] = set()
    data_type = (column.normalized_data_type or "").upper()

    if data_type in {"INTEGER", "DECIMAL"} and rule_type in _RELATIONSHIP_APPLICABLE_RULE_TYPES:
        applicable.add("RELATIONSHIP")
    if data_type in _SEQUENCE_APPLICABLE_TYPES and rule_type in _SEQUENCE_PROGRESSION_RULE_TYPES:
        applicable.add("SEQUENCE")
    if data_type in _TEMPORAL_APPLICABLE_TYPES and rule_type in _SEQUENCE_PROGRESSION_RULE_TYPES:
        applicable.add("TEMPORAL")
    if data_type in _TEMPLATE_APPLICABLE_TYPES and rule_type in _TEMPLATE_APPLICABLE_RULE_TYPES:
        applicable.add("TEMPLATE")
    return frozenset(applicable)


def _sequence_strategy_matches_rule_type(strategy: str, rule_type: str) -> bool:
    """A SEQUENCE_GAP/TEMPORAL_GAP result is only meaningful for a
    COMPLETENESS (missing value) issue; a SEQUENCE_NEXT_VALUE/
    TEMPORAL_NEXT_VALUE result is only meaningful for a
    UNIQUENESS/DUPLICATE (this value is repeated) issue. Prevents e.g. a
    gap found elsewhere in the column from being misapplied to a
    duplicate-resolution issue, or vice versa."""
    if strategy in {"SEQUENCE_GAP", "TEMPORAL_GAP"}:
        return rule_type in _GAP_LIKE_RULE_TYPES
    if strategy in {"SEQUENCE_NEXT_VALUE", "TEMPORAL_NEXT_VALUE"}:
        return rule_type in _NEXT_VALUE_LIKE_RULE_TYPES
    return False

# correction_suggestions.confidence is NOT NULL (frozen Phase 6 schema);
# generate_corrections() stores Decimal("0") whenever parsed["confidence"]
# is None (NEEDS_REVIEW/CANNOT_INFER, or an unparseable/failed response) —
# never a substitute for a real confidence value the model actually gave.


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
        self._prompt_service = PromptVersionService(db)
        self._audit = AuditingService(db)

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

    def _latest_column_profile(self, dataset_id: uuid.UUID, column_id: uuid.UUID) -> ColumnProfile | None:
        latest_run_id = self._db.execute(
            select(ProfileRun.id)
            .where(ProfileRun.dataset_id == dataset_id, ProfileRun.status == "COMPLETED")
            .order_by(ProfileRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_run_id is None:
            return None
        return self._db.execute(
            select(ColumnProfile).where(
                ColumnProfile.profile_run_id == latest_run_id, ColumnProfile.column_id == column_id
            )
        ).scalar_one_or_none()

    def _duplicate_examples(self, validation_failure: ValidationFailure) -> list[dict]:
        """Other rows in the SAME validation run whose failure was the exact
        same rule_assignment + failed_value — identifies "these records
        share this duplicate value" without exposing any other column's
        content, so the model can say "review which is authoritative"
        instead of inventing a replacement."""
        if not validation_failure.failed_value:
            return []
        rows = self._db.execute(
            select(ValidationResult.record_ref, ValidationResult.row_index)
            .join(ValidationFailure, ValidationFailure.validation_result_id == ValidationResult.id)
            .where(
                ValidationFailure.validation_run_id == validation_failure.validation_run_id,
                ValidationFailure.rule_assignment_id == validation_failure.rule_assignment_id,
                ValidationFailure.failed_value == validation_failure.failed_value,
                ValidationFailure.id != validation_failure.id,
            )
            .order_by(ValidationResult.row_index)
            .limit(_DUPLICATE_EXAMPLE_LIMIT)
        ).all()
        return [{"record_ref": record_ref, "row_index": row_index} for record_ref, row_index in rows]

    def _key_config_changed_since_validation_run(self, *, issue: Issue, dataset_id: uuid.UUID) -> bool:
        """Phase 4.6 safety: a dataset's key_strategy/dataset_key_columns
        can change AFTER a validation run already executed (most notably,
        a business key confirmed via BusinessKeyService.confirm() for a
        dataset that was previously ROW_INDEX_FALLBACK). That older run's
        stored Issue.record_ref values were generated under the OLD key
        strategy (see app.modules.validation.record_ref.generate_record_ref)
        — for ROW_INDEX_FALLBACK specifically they are "ROWIDX:N" strings
        that mean nothing under a newly-declared SINGLE_COLUMN/COMPOSITE
        strategy. parse_record_ref_to_key_dict() has no way to know this
        by itself; it only ever sees the dataset's CURRENT key_strategy.
        Reusing an existing, already-audited signal (the same
        dataset.key_columns_configured / dataset.business_key_confirmed
        audit events DatasetKeyService/BusinessKeyService already write)
        instead of adding a new column/migration: if the dataset's key
        configuration changed after this issue's validation run was
        created, treat the record_ref as untrustworthy for targeted
        retrieval — a fresh validation run is required before advanced
        evidence will use the new key for this data."""
        review_run = self._db.get(ReviewRun, issue.review_run_id)
        if review_run is None:
            return False
        validation_run = self._db.get(ValidationRun, review_run.validation_run_id)
        if validation_run is None:
            return False
        latest_key_change = self._db.execute(
            select(AuditEvent.created_at)
            .where(
                AuditEvent.entity_type == "DATASET",
                AuditEvent.entity_id == dataset_id,
                AuditEvent.action.in_(("dataset.key_columns_configured", "dataset.business_key_confirmed")),
            )
            .order_by(AuditEvent.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        return latest_key_change is not None and latest_key_change > validation_run.created_at

    def _gather_relationship_evidence(
        self, *, issue: Issue, column: Column, dataset_id: uuid.UUID,
    ) -> tuple[EvidenceResult | None, str | None]:
        """Phase 3: locates the exact failing row via a targeted,
        provider-agnostic provider.fetch_rows_by_keys() lookup keyed by the
        dataset's existing key strategy — no longer dependent on the failing
        row happening to appear in a bounded sample (Phase 1's limitation).
        Comparable rows still come from exactly ONE bounded
        provider.sample_rows() call (size settings.
        AI_CORRECTION_COMPARABLE_SAMPLE_SIZE) — unchanged from Phase 1,
        deliberately never a full-table scan.

        NEVER raises. Returns (None, reason) whenever evidence cannot be
        gathered for ANY reason: no reliable key strategy
        (ROW_INDEX_FALLBACK), a malformed/composite-mismatched record_ref, a
        provider that doesn't implement targeted retrieval
        (NotImplementedError — PostgreSQL is the only provider with a real
        implementation as of Phase 3; MySQL/Oracle/SAP HANA/SQL Server all
        raise NotImplementedError("Implemented in a later phase"), the same
        documented policy already applied to get_dataset_column_stats()),
        the record no longer existing at the source, credential/connection
        failure, or an unexpected error anywhere in this path. A None result
        never affects anything else: generate_corrections() proceeds exactly
        as it would if this feature didn't exist, just without an evidence
        block.

        Deliberately NO fallback to the old "search the bounded sample for
        the failing row" strategy when targeted retrieval is unsupported or
        fails — that would silently reintroduce the exact unreliability this
        phase exists to remove.

        Reuses the exact credential/provider path validation and profiling
        tasks already use (LocalRedisVaultClient + get_provider), and reuses
        staging's existing parse_record_ref_to_key_dict() (the exact inverse
        of generate_record_ref(), already proven against every provider's
        fetch_rows_by_keys() by StagingService._build_records) rather than
        inventing a second key-parsing implementation.
        """
        try:
            dataset = self._db.get(Dataset, dataset_id)
            if dataset is None:
                return None, "dataset_not_found"

            schema_row = self._db.get(Schema, dataset.schema_id)
            connection = self._db.get(Connection, schema_row.connection_id)
            connection_type = self._db.get(ConnectionType, connection.connection_type_id)

            active_columns = self._db.execute(
                select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
            ).scalars().all()
            columns_by_id = {c.id: c for c in active_columns}

            key_columns = self._db.execute(
                select(DatasetKeyColumn)
                .where(DatasetKeyColumn.dataset_id == dataset.id)
                .order_by(DatasetKeyColumn.ordinal)
            ).scalars().all()
            key_column_names = [
                columns_by_id[kc.column_id].name for kc in key_columns if kc.column_id in columns_by_id
            ]
            effective_key_strategy = dataset.key_strategy if key_column_names else "ROW_INDEX_FALLBACK"
            if effective_key_strategy == "ROW_INDEX_FALLBACK":
                return None, "no_reliable_key_strategy_for_targeted_row_retrieval"
            if self._key_config_changed_since_validation_run(issue=issue, dataset_id=dataset.id):
                return None, "key_configuration_changed_since_validation_run_stale_record_ref"

            key_dict = parse_record_ref_to_key_dict(
                issue.record_ref, key_strategy=effective_key_strategy, key_column_names=key_column_names,
            )
            if (
                key_dict is None
                or len(key_dict) != len(key_column_names)
                or any(v is None for v in key_dict.values())
            ):
                return None, "malformed_or_null_record_reference"

            redis_client = get_redis_client()
            vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)
            credential = vault.resolve(connection.credential_ref)
            provider = get_provider(
                connection_type.code, host=connection.host, port=connection.port,
                database=connection.database_name, username=credential.get("username", connection.username),
                password=credential.get("password", ""),
            )
            try:
                try:
                    fetched_rows = provider.fetch_rows_by_keys(schema_row.name, dataset.name, [key_dict])
                except NotImplementedError:
                    return None, "targeted_row_retrieval_not_supported_by_provider"

                if not fetched_rows:
                    return None, "record_not_found_at_source"
                if len(fetched_rows) > 1:
                    # A single-key lookup returning more than one row means
                    # the dataset's declared key strategy does not actually
                    # identify a unique record at the source (a key
                    # integrity problem, not a normal outcome) — never
                    # silently pick one; treat exactly like any other
                    # evidence-unavailable reason.
                    return None, "multiple_rows_matched_for_key_expected_unique"
                failing_row = fetched_rows[0]

                sample_result = provider.sample_rows(
                    schema_row.name, dataset.name, settings.AI_CORRECTION_COMPARABLE_SAMPLE_SIZE,
                    row_count_estimate=dataset.row_count_estimate,
                )
            finally:
                provider.close()

            rows = sample_result.rows
            column_metas = [
                _EvidenceColumnMeta(
                    name=c.name, normalized_data_type=c.normalized_data_type, is_primary_key=c.is_primary_key,
                    distinct_percentage=self._distinct_percentage(dataset.id, c.id),
                )
                for c in active_columns
            ]

            result = discover_relationship_evidence(
                target_column=column.name, failing_row=failing_row, candidate_rows=rows, columns=column_metas,
                min_group_size=settings.AI_CORRECTION_MIN_COMPARABLE_GROUP_SIZE,
                min_fit_quality=settings.AI_CORRECTION_MIN_FIT_QUALITY,
            )
            return result, None
        except Exception as exc:  # noqa: BLE001 — deliberate: evidence gathering must never break correction generation
            return None, f"{type(exc).__name__}: {exc}"

    def _gather_advanced_evidence(
        self, *, issue: Issue, column: Column, rule_type: str, dataset_id: uuid.UUID,
    ) -> tuple[AggregationResult | None, str | None]:
        """Phase 4.5. Deliberately duplicates
        _gather_relationship_evidence()'s targeted-row-retrieval +
        bounded-sample logic independently, rather than refactoring it into
        a shared helper — this keeps the already-approved, already-tested
        Phase 1-3 path (used whenever AI_CORRECTION_ADVANCED_INFERENCE_ENABLED
        is False) completely untouched and at zero regression risk. Only
        called when that flag is True.

        Runs whichever of the four Phase 4 evidence engines
        _applicable_strategies() says are relevant for this column/rule_type,
        converts each engine's CANDIDATE-status result into one or more
        app.modules.ai.candidates.Candidate objects, and ranks them via
        aggregate_candidates(). Never raises — returns (None, reason) for
        the same universe of failure reasons _gather_relationship_evidence()
        documents (no reliable key strategy, malformed record ref,
        unsupported provider, record not found, credential/connection
        failure, unexpected error) — a None result never affects anything
        else, exactly like the Phase 1-3 path.

        Failing-row exclusion: RELATIONSHIP evidence excludes the failing
        row internally (discover_relationship_evidence's own `pool`
        construction). TEMPLATE evidence excludes it from comparable_pairs
        (its own target value may be invalid/malformed and must never be
        trained on) and uses it only as the query_source. SEQUENCE/TEMPORAL
        evidence deliberately INCLUDE it (deduplicated so it is counted
        exactly once regardless of whether the bounded sample happened to
        already contain it) — a duplicate-resolution candidate is only
        detectable by actually seeing the duplication in the observed
        value set; excluding the failing row would hide the very anomaly
        being resolved. For a missing/null target (COMPLETENESS), this is
        moot — a null value is filtered out by each engine's own null
        handling regardless.
        """
        try:
            dataset = self._db.get(Dataset, dataset_id)
            if dataset is None:
                return None, "dataset_not_found"

            schema_row = self._db.get(Schema, dataset.schema_id)
            connection = self._db.get(Connection, schema_row.connection_id)
            connection_type = self._db.get(ConnectionType, connection.connection_type_id)

            active_columns = self._db.execute(
                select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
            ).scalars().all()
            columns_by_id = {c.id: c for c in active_columns}

            key_columns = self._db.execute(
                select(DatasetKeyColumn)
                .where(DatasetKeyColumn.dataset_id == dataset.id)
                .order_by(DatasetKeyColumn.ordinal)
            ).scalars().all()
            key_column_names = [
                columns_by_id[kc.column_id].name for kc in key_columns if kc.column_id in columns_by_id
            ]
            effective_key_strategy = dataset.key_strategy if key_column_names else "ROW_INDEX_FALLBACK"
            if effective_key_strategy == "ROW_INDEX_FALLBACK":
                return None, "no_reliable_key_strategy_for_targeted_row_retrieval"
            if self._key_config_changed_since_validation_run(issue=issue, dataset_id=dataset.id):
                return None, "key_configuration_changed_since_validation_run_stale_record_ref"

            key_dict = parse_record_ref_to_key_dict(
                issue.record_ref, key_strategy=effective_key_strategy, key_column_names=key_column_names,
            )
            if (
                key_dict is None
                or len(key_dict) != len(key_column_names)
                or any(v is None for v in key_dict.values())
            ):
                return None, "malformed_or_null_record_reference"

            redis_client = get_redis_client()
            vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)
            credential = vault.resolve(connection.credential_ref)
            provider = get_provider(
                connection_type.code, host=connection.host, port=connection.port,
                database=connection.database_name, username=credential.get("username", connection.username),
                password=credential.get("password", ""),
            )
            try:
                try:
                    fetched_rows = provider.fetch_rows_by_keys(schema_row.name, dataset.name, [key_dict])
                except NotImplementedError:
                    return None, "targeted_row_retrieval_not_supported_by_provider"

                if not fetched_rows:
                    return None, "record_not_found_at_source"
                if len(fetched_rows) > 1:
                    # A single-key lookup returning more than one row means
                    # the dataset's declared key strategy does not actually
                    # identify a unique record at the source (a key
                    # integrity problem, not a normal outcome) — never
                    # silently pick one; treat exactly like any other
                    # evidence-unavailable reason.
                    return None, "multiple_rows_matched_for_key_expected_unique"
                failing_row = fetched_rows[0]

                sample_result = provider.sample_rows(
                    schema_row.name, dataset.name, settings.AI_CORRECTION_COMPARABLE_SAMPLE_SIZE,
                    row_count_estimate=dataset.row_count_estimate,
                )
            finally:
                provider.close()

            rows = sample_result.rows
            applicable = _applicable_strategies(column, rule_type)
            strategies_attempted: list[str] = []
            candidates: list[Candidate] = []

            if "RELATIONSHIP" in applicable:
                strategies_attempted.append("RELATIONSHIP")
                column_metas = [
                    _EvidenceColumnMeta(
                        name=c.name, normalized_data_type=c.normalized_data_type, is_primary_key=c.is_primary_key,
                        distinct_percentage=self._distinct_percentage(dataset.id, c.id),
                    )
                    for c in active_columns
                ]
                rel_result = discover_relationship_evidence(
                    target_column=column.name, failing_row=failing_row, candidate_rows=rows, columns=column_metas,
                    min_group_size=settings.AI_CORRECTION_MIN_COMPARABLE_GROUP_SIZE,
                    min_fit_quality=settings.AI_CORRECTION_MIN_FIT_QUALITY,
                )
                if rel_result.status == "CANDIDATE":
                    candidates.append(
                        Candidate(
                            value=_format_number(rel_result.best.candidate_value),
                            strategy=rel_result.best.relationship_type,
                            confidence=rel_result.best.confidence,
                            supporting_count=rel_result.best.comparable_group_size,
                            contradicting_count=0,
                            evidence={
                                "related_columns": list(rel_result.best.related_columns),
                                "fit_quality": round(rel_result.best.fit_quality, 4),
                            },
                        )
                    )

            # SEQUENCE/TEMPORAL: the failing row's own value is included
            # back into the pool (deduplicated) — see docstring above.
            other_rows = [r for r in rows if r != failing_row]
            own_column_values = [r.get(column.name) for r in other_rows] + [failing_row.get(column.name)]

            if "SEQUENCE" in applicable:
                strategies_attempted.append("SEQUENCE")
                seq_result = discover_sequence_evidence(target_column=column.name, observed_values=own_column_values)
                if seq_result.status == "CANDIDATE" and _sequence_strategy_matches_rule_type(
                    seq_result.best.strategy, rule_type
                ):
                    candidates.append(
                        Candidate(
                            value=seq_result.best.candidate_value, strategy=seq_result.best.strategy,
                            confidence=seq_result.best.confidence, supporting_count=seq_result.best.supporting_count,
                            contradicting_count=seq_result.best.contradicting_count,
                            evidence={
                                "progression": seq_result.best.observed_pattern,
                                "gap_count": seq_result.best.gap_count,
                                "duplicate_count": seq_result.best.duplicate_count,
                            },
                        )
                    )

            if "TEMPORAL" in applicable:
                strategies_attempted.append("TEMPORAL")
                temp_result = discover_temporal_evidence(target_column=column.name, observed_values=own_column_values)
                if temp_result.status == "CANDIDATE" and _sequence_strategy_matches_rule_type(
                    temp_result.best.strategy, rule_type
                ):
                    candidates.append(
                        Candidate(
                            value=temp_result.best.candidate_value, strategy=temp_result.best.strategy,
                            confidence=temp_result.best.confidence, supporting_count=temp_result.best.supporting_count,
                            contradicting_count=temp_result.best.contradicting_count,
                            evidence={
                                "progression_type": temp_result.best.progression_type,
                                "interval": temp_result.best.interval,
                                "gap_count": temp_result.best.gap_count,
                                "duplicate_count": temp_result.best.duplicate_count,
                            },
                        )
                    )

            if "TEMPLATE" in applicable:
                strategies_attempted.append("TEMPLATE")
                # Every OTHER active STRING/TEXT column is tried as a
                # candidate source, one at a time — deliberately never
                # assuming which column (if any) "explains" this one.
                # comparable_pairs excludes the failing row (its own
                # target value may be exactly the invalid one under
                # review and must never be trained on) — AND every other
                # row that itself has an open Issue on this same target
                # column in this review run. A sibling row under review
                # for the identical column is exactly as untrustworthy as
                # training data as the current row's own value; without
                # this exclusion, one bad row's malformed value sits in
                # the "known good" pool and can single-handedly contradict
                # (and so suppress) an otherwise-unanimous template that
                # every genuinely clean row agrees on.
                sibling_bad_record_refs = set(
                    self._db.execute(
                        select(Issue.record_ref).where(
                            Issue.review_run_id == issue.review_run_id,
                            Issue.column_id == column.id,
                            Issue.id != issue.id,
                        )
                    ).scalars()
                )
                template_training_rows = [
                    r for r in other_rows
                    if generate_record_ref(
                        key_strategy=effective_key_strategy, key_column_names_in_order=key_column_names,
                        row=r, row_index=0,
                    ) not in sibling_bad_record_refs
                ] if sibling_bad_record_refs else other_rows
                source_columns = [
                    c for c in active_columns
                    if c.id != column.id and (c.normalized_data_type or "").upper() in {"STRING", "TEXT"}
                ]
                for source_column in source_columns:
                    pairs = [
                        ((r.get(source_column.name),), r.get(column.name))
                        for r in template_training_rows
                        if r.get(column.name) is not None and str(r.get(column.name)).strip() != ""
                    ]
                    query_source = (failing_row.get(source_column.name),)
                    tmpl_result = discover_template_evidence(
                        target_column=column.name, related_columns=(source_column.name,),
                        comparable_pairs=pairs, query_source=query_source,
                    )
                    if tmpl_result.status == "CANDIDATE":
                        candidates.append(
                            Candidate(
                                value=tmpl_result.best.candidate_value, strategy=tmpl_result.best.strategy,
                                confidence=tmpl_result.best.confidence,
                                supporting_count=tmpl_result.best.supporting_count,
                                contradicting_count=tmpl_result.best.contradicting_count,
                                evidence={
                                    "related_columns": list(tmpl_result.best.related_columns),
                                    "template": tmpl_result.best.template,
                                },
                            )
                        )

            aggregation = aggregate_candidates(candidates, strategies_attempted=strategies_attempted)
            return aggregation, None
        except Exception as exc:  # noqa: BLE001 — deliberate: evidence gathering must never break correction generation
            return None, f"{type(exc).__name__}: {exc}"

    def _distinct_percentage(self, dataset_id: uuid.UUID, column_id: uuid.UUID) -> float | None:
        profile = self._latest_column_profile(dataset_id, column_id)
        if profile is None or profile.distinct_percentage is None:
            return None
        return float(profile.distinct_percentage)

    def _correction_context(
        self, issue: Issue, actor: User
    ) -> tuple[dict, EvidenceResult | None, AggregationResult | None]:
        validation_failure = self._db.get(ValidationFailure, issue.validation_failure_id)
        rule_assignment = self._db.get(RuleAssignment, validation_failure.rule_assignment_id)
        rule_version = self._db.get(RuleVersion, rule_assignment.rule_version_id)
        rule = self._db.get(Rule, rule_version.rule_id)
        column = self._db.get(Column, issue.column_id) if issue.column_id else None

        review_run = self._db.get(ReviewRun, issue.review_run_id)
        validation_run = self._db.get(ValidationRun, review_run.validation_run_id)
        column_profile = (
            self._latest_column_profile(validation_run.dataset_id, issue.column_id) if issue.column_id else None
        )
        duplicate_examples = (
            self._duplicate_examples(validation_failure) if rule.rule_type in _UNIQUENESS_LIKE_RULE_TYPES else []
        )

        evidence_result: EvidenceResult | None = None
        evidence_summary: dict | None = None
        aggregation: AggregationResult | None = None
        advanced_summary: dict | None = None

        # AI_CORRECTION_ADVANCED_INFERENCE_ENABLED takes priority over the
        # Phase 1-3 single-strategy path when both flags are on — the two
        # are never combined for the same issue (that would mean sending
        # the LLM two overlapping, possibly-conflicting evidence blocks).
        # Flag OFF (the default): this whole block is skipped exactly as
        # before Phase 4.5 existed — see the `elif` branch below, byte-for-
        # byte the same code path Phase 1-3 already established.
        if settings.AI_CORRECTION_ADVANCED_INFERENCE_ENABLED and column is not None:
            aggregation, reason = self._gather_advanced_evidence(
                issue=issue, column=column, rule_type=rule.rule_type, dataset_id=validation_run.dataset_id,
            )
            advanced_summary = _advanced_evidence_summary(aggregation, reason_if_none=reason)
            self._audit.record(
                actor=actor, action="ai_correction.advanced_evidence_gathered", entity_type="ISSUE", entity_id=issue.id,
                metadata={
                    "advanced_inference_enabled": True,
                    "evidence_available": advanced_summary["available"],
                    "recommended_strategy": advanced_summary.get("recommended_strategy"),
                    "ambiguous": advanced_summary.get("ambiguous"),
                    "strategies_attempted": advanced_summary.get("strategies_attempted"),
                    "strategies_agreeing": advanced_summary.get("strategies_agreeing"),
                },
            )
            self._db.commit()
        elif settings.AI_CORRECTION_EVIDENCE_ENABLED and column is not None:
            evidence_result, reason = self._gather_relationship_evidence(
                issue=issue, column=column, dataset_id=validation_run.dataset_id,
            )
            evidence_summary = _evidence_summary(evidence_result, reason_if_none=reason)
            # Structural diagnostics only — never raw source rows or values.
            self._audit.record(
                actor=actor, action="ai_correction.evidence_gathered", entity_type="ISSUE", entity_id=issue.id,
                metadata={
                    "evidence_enabled": True,
                    "evidence_available": evidence_summary["available"],
                    "evidence_status": evidence_summary.get("status"),
                    "relationship_type": evidence_summary.get("relationship_type"),
                    "group_size": evidence_summary.get("group_size"),
                    "fit_quality": evidence_summary.get("fit_quality"),
                },
            )
            # Committed immediately so this audit trail survives even if the
            # subsequent LLM call fails and the per-issue handler rolls back.
            self._db.commit()

        context = build_correction_context(
            issue=issue, column=column, validation_failure=validation_failure, rule=rule,
            rule_version=rule_version, column_profile=column_profile, duplicate_examples=duplicate_examples,
            relationship_evidence=evidence_summary, advanced_evidence=advanced_summary,
        )
        return context, evidence_result, aggregation

    def generate_corrections(self, review_run_id: uuid.UUID, actor: User) -> list[AISuggestion]:
        """One ai_suggestions row PER issue, each immediately bridged into a
        correction_suggestions row (source="AI", ai_suggestion_id=<the new
        id>) — a plain INSERT using the existing Phase 6 model. Only issues
        without an existing correction_suggestions row are considered,
        mirroring generate_for_review_run()'s own already-suggested
        exclusion — the deterministic registry (tried via that other
        service) always wins when it produces a result; this only fills
        the gaps: rule types the registry has no generator for, and issues
        its generators declined for lack of evidence.

        Every target issue in the review run is attempted here — no
        rule_type filter, no count cap. Each issue's AI call is isolated in
        its own try/except: a failure for one issue (provider error,
        unparseable response, AI disabled) is recorded as a CANNOT_INFER
        suggestion for THAT issue only and never aborts the remaining
        issues in the batch — comprehensiveness must not depend on every
        single call succeeding.

        Phase 4.10: mirrors SuggestionService.generate_for_review_run's own
        Phase 7 DRAFT -> IN_REVIEW trigger. Beginning review work through
        this AI-only path is exactly as much "starting the review" as the
        deterministic path is — before this fix, only generate_for_review_run
        performed this transition, so a review run whose only review action
        was ever "Generate AI Suggestions" stayed DRAFT forever, even after
        every issue was decided (ApprovalService.submit() requires
        IN_REVIEW). Committed immediately, independent of whether any
        target issues actually exist below, so the transition is durable
        even on a review run with zero PENDING issues at call time.
        """
        review_run = self._db.get(ReviewRun, review_run_id)
        if review_run is not None and review_run.status == "DRAFT":
            review_run.status = "IN_REVIEW"
            self._db.commit()

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
            # The schema invariant ck_correction_suggestions_ai_suggestion_id_shape
            # requires every source='AI' row to reference a real ai_suggestions
            # row — even a failed attempt is logged as one (content describing
            # the error), so a per-issue failure still gets full provenance,
            # never a dangling/null ai_suggestion_id.
            strategy_to_persist: str | None = None
            evidence_detail_to_persist: dict | None = None
            # Phase 4.9 traceability: set the moment a real orchestrator
            # call actually completes (success OR AIProviderUnavailableError
            # — see that exception's attached usage_log_id), and never
            # touched again afterward even if a LATER step in this same try
            # block fails — so a real, already-logged provider call is
            # still linked to whichever ai_suggestions row this attempt
            # ultimately produces, degraded fallback included. Stays None
            # when no orchestrator call was ever made for this issue (e.g.
            # _correction_context itself raised) — never backfilled onto a
            # suggestion that has no real usage log to link.
            usage_log_id_for_backfill: uuid.UUID | None = None
            try:
                context, evidence_result, aggregation = self._correction_context(issue, actor)
                result = self._orchestrator.run(prompt_key=_PROMPT_KEY_CORRECTION, context=context, actor=actor)
                usage_log_id_for_backfill = result.usage_log_id
                parsed = _parse_correction_response(result.text)
                # Backend safety enforcement — applies regardless of what the
                # prompt says: the Evidence Engine(s) (not the LLM) own
                # candidate_value/the safety decision. Exactly one of these
                # two calls is ever non-no-op for a given issue (evidence_result
                # and aggregation are never both non-None — see
                # _correction_context's mutually-exclusive branching).
                parsed = _apply_evidence_safety(parsed, evidence_result)
                parsed = _apply_advanced_evidence_safety(parsed, aggregation)
                content = {"text": result.text, "parsed": parsed}
                if context.get("relationship_evidence") is not None:
                    content["relationship_evidence"] = context["relationship_evidence"]
                if context.get("advanced_evidence") is not None:
                    content["advanced_evidence"] = context["advanced_evidence"]
                if aggregation is not None:
                    strategy_to_persist = (
                        aggregation.recommended_candidate.strategy if aggregation.recommended_candidate else None
                    )
                    evidence_detail_to_persist = context.get("advanced_evidence")
                provider, model = result.provider, result.model
                prompt_version_id = result.prompt_version.id
                context_hash = result.context_hash
            except Exception as exc:  # noqa: BLE001 — deliberate: isolate one issue's failure from the batch
                self._db.rollback()
                if usage_log_id_for_backfill is None:
                    usage_log_id_for_backfill = getattr(exc, "usage_log_id", None)
                parsed = {
                    "category": "CANNOT_INFER", "suggested_value": None, "confidence": None,
                    "reasoning": f"AI reasoning unavailable for this issue: {type(exc).__name__}: {exc}",
                }
                content = {"error": f"{type(exc).__name__}: {exc}"}
                provider, model = "unavailable", "unavailable"
                context_hash = "unavailable"
                try:
                    prompt_version_id = self._prompt_service.resolve_active(_PROMPT_KEY_CORRECTION).id
                except Exception:
                    # No active ai_correction prompt version configured at
                    # all — a configuration problem, not a per-issue AI
                    # reasoning failure. Can't create a valid ai_suggestions
                    # row without violating its own NOT NULL FK, so this one
                    # issue is left for the next run rather than writing a
                    # row that breaks provenance.
                    continue

            suggestion = self._create_suggestion(
                suggestion_type="CORRECTION", source_context_type="ISSUE", source_context_id=issue.id,
                content=content, provider=provider, model=model, prompt_version_id=prompt_version_id,
                conversation_id=None, actor=actor, context_hash=context_hash,
            )

            if usage_log_id_for_backfill is not None:
                # Phase 4.9: complete the ai_usage_logs -> ai_suggestions
                # link now that the suggestion this attempt produced
                # actually has an id (see AIOrchestratorService.run's own
                # ai_suggestion_id_for_usage_log parameter's docstring for
                # why this can never be passed in ahead of time — the
                # suggestion row doesn't exist yet when the provider call
                # happens). Never fabricated: only ever set when a real
                # usage log for THIS attempt genuinely exists.
                usage_log = self._db.get(AIUsageLog, usage_log_id_for_backfill)
                if usage_log is not None:
                    usage_log.ai_suggestion_id = suggestion.id

            # The one and only write into any Phase 6 table anywhere in
            # this implementation — a plain INSERT, no call into
            # SuggestionService/CorrectionDecisionService. suggested_value
            # is "" (never a fabricated string) whenever no value was
            # proposed — NEEDS_REVIEW/CANNOT_INFER, including failures.
            # strategy/evidence_detail (Phase 4.1 schema) stay NULL unless
            # the advanced path actually ran for this issue.
            self._db.add(
                CorrectionSuggestion(
                    issue_id=issue.id, source="AI", ai_suggestion_id=suggestion.id,
                    suggested_value=parsed["suggested_value"] or "",
                    confidence=(
                        Decimal(str(parsed["confidence"])) if parsed["confidence"] is not None else Decimal("0")
                    ),
                    category=parsed["category"], fix_type="AI_PROPOSED", reasoning=parsed["reasoning"],
                    is_selected=False, strategy=strategy_to_persist, evidence_detail=evidence_detail_to_persist,
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


def _strip_markdown_fence(text: str) -> str:
    """Strips a ```json ... ``` (or bare ``` ... ```) code fence if present —
    models commonly wrap JSON output in one even when told not to."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return cleaned


def _parse_rule_recommendations(text: str) -> list[dict]:
    """Defensive parse of the LLM's JSON response — requires a JSON array
    after stripping any code fence. Returns [] (not an error) for a
    syntactically valid empty array — "no column here warrants a rule" is
    a legitimate answer, not a failure. Raises AIResponseInvalidError only
    when the response can't be interpreted as the requested shape at all,
    since nothing downstream can safely act on an unparseable proposal —
    matches this project's existing AIResponseInvalidError semantics
    (HTTP 502, not swallowed silently)."""
    cleaned = _strip_markdown_fence(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AIResponseInvalidError(f"Rule recommendation response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise AIResponseInvalidError("Rule recommendation response was not a JSON array")
    return [item for item in parsed if isinstance(item, dict)]


def _parse_correction_response(text: str) -> dict:
    """Defensive parse of the correction prompt's structured JSON response
    — {"category": ..., "suggested_value": ... | null, "confidence":
    number | null, "reasoning": ...}. Unlike _parse_rule_recommendations,
    this NEVER raises: a single issue's unparseable/malformed response
    must degrade to an explicit NEEDS_REVIEW/CANNOT_INFER outcome with
    suggested_value="" (never a fabricated value) rather than aborting the
    whole batch — generate_corrections() still wraps the call site in its
    own try/except for provider-level failures (this function only
    handles response-shape failures)."""
    fallback = {
        "category": "NEEDS_REVIEW",
        "suggested_value": None,
        "confidence": None,
        "reasoning": None,
    }
    cleaned = _strip_markdown_fence(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        fallback["reasoning"] = f"AI response was not valid structured JSON: {text[:500]!r}"
        return fallback
    if not isinstance(parsed, dict):
        fallback["reasoning"] = f"AI response was not a JSON object: {text[:500]!r}"
        return fallback

    category = parsed.get("category")
    if category not in _AI_CORRECTION_CATEGORIES:
        fallback["reasoning"] = (
            f"AI response used an unrecognized category {category!r}; "
            f"original reasoning: {parsed.get('reasoning')!r}"
        )
        return fallback

    suggested_value = parsed.get("suggested_value")
    if not isinstance(suggested_value, str) or not suggested_value.strip():
        suggested_value = None
    # A NEEDS_REVIEW/CANNOT_INFER category must never carry a value, even if
    # the model provided one anyway — the category is the authoritative
    # signal, and inconsistency here must resolve toward NOT guessing.
    if category != "AI_HIGH_CONFIDENCE":
        suggested_value = None

    # Self-correcting inconsistency guard: a category can never claim
    # AI_HIGH_CONFIDENCE without an actual value to back it up — resolve
    # toward NOT guessing rather than storing a confident-looking category
    # next to an empty suggested_value.
    if category == "AI_HIGH_CONFIDENCE" and suggested_value is None:
        category = "NEEDS_REVIEW"

    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
        confidence = None
    if category != "AI_HIGH_CONFIDENCE":
        confidence = None

    reasoning = parsed.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, str) and reasoning.strip() else None

    return {
        "category": category,
        "suggested_value": suggested_value,
        "confidence": confidence,
        "reasoning": reasoning,
    }


# --- Phase 1: relationship-evidence integration glue -----------------------
# (app.modules.ai.evidence stays pure/independent — this glue is deliberately
# kept here, not there, since it's specific to wiring evidence into the
# CORRECTION prompt/response, not part of the Evidence Engine itself.)


def _evidence_summary(result: "EvidenceResult | None", *, reason_if_none: str | None) -> dict:
    """The aggregate-only structure sent to the LLM and stored in
    ai_suggestions.content — never raw comparable rows or any other
    column's actual values. When available, numbers here are rounded for
    readability only; the authoritative candidate_value used for the
    safety cross-check in _apply_evidence_safety() always comes straight
    from the EvidenceResult object, never from this rounded copy."""
    if result is None:
        return {"available": False, "reason": reason_if_none or "Evidence gathering was not attempted."}

    if result.status != "CANDIDATE" or result.best is None:
        return {
            "available": True,
            "status": result.status,
            "group_size": result.comparable_group_size,
            "reason": result.reason,
        }

    best = result.best
    return {
        "available": True,
        "status": result.status,
        "relationship_type": best.relationship_type,
        "group_size": best.comparable_group_size,
        "fit_quality": round(best.fit_quality, 4),
        "residual": round(best.residual, 6),
        "coefficient_of_variation": (
            round(best.coefficient_of_variation, 6) if best.coefficient_of_variation is not None else None
        ),
        "candidate_value": best.candidate_value,
        "related_columns": list(best.related_columns),
        "reason": result.reason,
    }


def _try_parse_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(round(value, 6))


def _apply_evidence_safety(parsed: dict, evidence_result: "EvidenceResult | None") -> dict:
    """Enforced in code, never left to prompt wording alone. Runs
    regardless of what the LLM said:

    - No evidence gathered at all (flag off, or unavailable for this
      issue) -> no-op, exactly today's behavior.
    - The AI did NOT claim AI_HIGH_CONFIDENCE -> no-op; NEEDS_REVIEW/
      CANNOT_INFER from the model are left as-is regardless of evidence.
    - The AI claimed AI_HIGH_CONFIDENCE but the Evidence Engine's own
      status wasn't CANDIDATE (NO_RELATIONSHIP/INSUFFICIENT_GROUP/
      AMBIGUOUS) -> reject the AI's own value outright, downgrade to
      NEEDS_REVIEW. The AI is never allowed to be more confident than the
      deterministic evidence supports.
    - The AI claimed AI_HIGH_CONFIDENCE and the Evidence Engine found a
      real CANDIDATE, but the AI's own suggested_value doesn't match the
      deterministic candidate_value (within tolerance) -> reject the AI's
      value, downgrade to NEEDS_REVIEW. The deterministic candidate is
      never silently swapped in as if the AI had proposed it.
    - Only when the AI's value agrees with the deterministic candidate is
      AI_HIGH_CONFIDENCE kept — and even then, suggested_value is
      overwritten with the backend's own candidate_value (never the LLM's
      own string rendering) and confidence is capped at whatever the
      Evidence Engine itself computed, never higher.
    """
    if evidence_result is None or parsed["category"] != "AI_HIGH_CONFIDENCE":
        return parsed

    if evidence_result.status != "CANDIDATE" or evidence_result.best is None:
        return {
            "category": "NEEDS_REVIEW",
            "suggested_value": None,
            "confidence": None,
            "reasoning": (
                f"AI proposed a high-confidence correction, but deterministic evidence analysis found "
                f"{evidence_result.status} ({evidence_result.reason}) — rejecting the AI's own value and "
                "requiring human review."
            ),
        }

    candidate = evidence_result.best.candidate_value
    llm_value = _try_parse_float(parsed["suggested_value"])
    if llm_value is None or not math.isclose(llm_value, candidate, rel_tol=1e-3, abs_tol=1e-6):
        return {
            "category": "NEEDS_REVIEW",
            "suggested_value": None,
            "confidence": None,
            "reasoning": (
                f"AI proposed {parsed['suggested_value']!r}, which does not match the deterministic evidence "
                f"candidate ({candidate!r}) from {evidence_result.best.relationship_type} — rejecting the AI "
                "value and requiring human review. The deterministic candidate is preserved separately in "
                "this suggestion's evidence."
            ),
        }

    updated = dict(parsed)
    updated["suggested_value"] = _format_number(candidate)
    updated["confidence"] = (
        min(parsed["confidence"], evidence_result.best.confidence)
        if parsed["confidence"] is not None
        else evidence_result.best.confidence
    )
    return updated


# --- Phase 4.5: advanced multi-strategy evidence integration glue ----------
# (app.modules.ai.candidates / evidence_sequence / evidence_template /
# evidence_temporal all stay pure/independent — this glue is deliberately
# kept here, not there, same reasoning as the Phase 1 section above.)


def _advanced_evidence_summary(result: "AggregationResult | None", *, reason_if_none: str | None) -> dict:
    """The aggregate-only structure sent to the LLM (as context["advanced_evidence"])
    and stored in ai_suggestions.content / correction_suggestions.evidence_detail —
    never raw comparable rows/pairs or any other column's actual values.
    Mirrors _evidence_summary()'s shape/privacy posture for the
    single-strategy case, extended for a ranked multi-strategy result."""
    if result is None:
        return {"available": False, "reason": reason_if_none or "Advanced evidence gathering was not attempted."}

    candidate = result.recommended_candidate
    return {
        "available": True,
        "ambiguous": result.ambiguous,
        "strategies_attempted": list(result.strategies_attempted),
        "strategies_agreeing": list(result.strategies_with_candidates),
        "recommended_candidate": candidate.value if candidate is not None else None,
        "recommended_strategy": candidate.strategy if candidate is not None else None,
        "confidence": round(candidate.confidence, 4) if candidate is not None else None,
        "supporting_count": candidate.supporting_count if candidate is not None else None,
        "contradicting_count": candidate.contradicting_count if candidate is not None else None,
        "reason": result.no_candidate_reason,
    }


def _apply_advanced_evidence_safety(parsed: dict, aggregation: "AggregationResult | None") -> dict:
    """Enforced in code, never left to prompt wording alone — the Phase 4.5
    analogue of _apply_evidence_safety() for the aggregated, multi-strategy
    candidate set. A no-op whenever aggregation is None (advanced inference
    disabled, or not applicable/available for this issue) — in that case
    _apply_evidence_safety() (already applied first, see generate_corrections())
    is the only safety net in effect, exactly as before Phase 4.5 existed.

    Rules (mirrors the BACKEND SAFETY CHECK / CATEGORIES sections of the
    Phase 4.5 spec):
    - No recommended candidate at all, OR the aggregation is ambiguous ->
      suggested_value is forced null regardless of what the AI said; a
      category of AI_HIGH_CONFIDENCE is downgraded to NEEDS_REVIEW (never
      confident about ambiguous/absent evidence) — NEEDS_REVIEW/
      CANNOT_INFER from the AI are otherwise left as its own judgement.
    - A real recommended_candidate exists:
        - AI_HIGH_CONFIDENCE whose own suggested_value does not match the
          deterministic candidate (or gave none) -> rejected outright,
          downgraded to NEEDS_REVIEW — but suggested_value is still
          populated with the backend's own candidate (never left blank
          when the backend has a real, defensible answer).
        - AI_HIGH_CONFIDENCE whose value agrees -> kept, suggested_value
          overwritten with the backend's exact string, confidence capped
          at the candidate's own confidence.
        - NEEDS_REVIEW from the AI (declining despite a candidate
          existing) -> respected as the AI's own more conservative
          judgement call (never silently upgraded), but suggested_value is
          still populated with the backend candidate — a real deterministic
          value is available for the reviewer even though the AI itself
          wants a human to confirm it (this is the one intentional
          behavior change from Phase 1-3: NEEDS_REVIEW may now carry a
          value, whenever recommended_candidate is not None).
        - CANNOT_INFER from the AI -> the AI's own "no usable signal"
          self-assessment is inconsistent with a real backend candidate
          existing; upgraded to NEEDS_REVIEW (never AI_HIGH_CONFIDENCE)
          with the candidate populated, for the same reason.
    Suggested value, whenever populated, is always exactly the backend's
    own candidate string — never the LLM's own rendering of it.
    """
    if aggregation is None:
        return parsed

    candidate = aggregation.recommended_candidate
    if aggregation.ambiguous or candidate is None:
        category = "NEEDS_REVIEW" if parsed["category"] == "AI_HIGH_CONFIDENCE" else parsed["category"]
        return {
            "category": category,
            "suggested_value": None,
            "confidence": None,
            "reasoning": (
                parsed["reasoning"]
                or aggregation.no_candidate_reason
                or "No defensible deterministic candidate is available for this issue."
            ),
        }

    category = parsed["category"]
    reasoning = parsed["reasoning"]
    if category == "AI_HIGH_CONFIDENCE":
        llm_value = parsed["suggested_value"]
        if llm_value is None or not _values_match(llm_value, candidate.value):
            category = "NEEDS_REVIEW"
            reasoning = (
                f"AI proposed {llm_value!r}, which does not match the deterministic candidate "
                f"({candidate.value!r}) from {candidate.strategy} — rejecting the AI value; the deterministic "
                "candidate is preserved for human review."
            )
    elif category == "CANNOT_INFER":
        category = "NEEDS_REVIEW"
        reasoning = reasoning or (
            f"Deterministic {candidate.strategy} evidence provides a candidate even though the AI reported no "
            "usable signal — presenting it for human review rather than discarding it."
        )
    # NEEDS_REVIEW from the AI is left as its own judgement call — never
    # silently upgraded to AI_HIGH_CONFIDENCE just because a candidate
    # exists (mirrors Phase 2's "AI may decline a candidate" guarantee).

    confidence = None
    if category == "AI_HIGH_CONFIDENCE":
        confidence = (
            min(parsed["confidence"], candidate.confidence) if parsed["confidence"] is not None else candidate.confidence
        )

    return {
        "category": category,
        "suggested_value": candidate.value,
        "confidence": confidence,
        "reasoning": reasoning,
    }


def _values_match(a: str, b: str) -> bool:
    """String equality with a float-aware fallback (so "735" and "735.0"
    are never mistaken for a disagreement) — mirrors
    app.modules.ai.candidates._values_equal's semantics, reimplemented
    here using this module's own _try_parse_float for style consistency
    with _apply_evidence_safety's existing math.isclose-based comparison."""
    if a == b:
        return True
    fa, fb = _try_parse_float(a), _try_parse_float(b)
    if fa is None or fb is None:
        return False
    return math.isclose(fa, fb, rel_tol=1e-3, abs_tol=1e-6)

"""Context assembly + deterministic input-context hashing.

Input boundary (Phase 12 decision, narrowed for CORRECTION suggestions —
see below): allowed context is metadata only — validation rule
definitions/metadata, validation failure metadata, column metadata,
profiling statistics, dataset metadata, review issue metadata, correction
context, aggregated quality statistics. Never sent to the provider: full
raw database rows, row_snapshot, passwords, credentials, connection
strings, API keys, secrets, or other columns' values.

build_issue_context() (EXPLANATION) keeps the original, narrower boundary
exactly as before. build_correction_context() (CORRECTION) deliberately
includes the single failing cell's own failed_value/expected_value and
aggregate profiling statistics (null/distinct/duplicate percentage,
mode/median/mean where present) — a product decision, not an oversight:
a correction proposal is structurally unable to reason about "what should
this value actually be" from severity/reason metadata alone, and
failed_value is already shown to any reviewer in the existing Review UI
(ValidationFailureResponse.failed_value), so this isn't new exposure of
data a reviewer couldn't already see. Still never sent: row_snapshot,
other columns' values, full value_distribution/top-values lists, or
anything credential-shaped.
"""
import hashlib
import json
from typing import Any


def compute_input_context_hash(context: dict[str, Any]) -> str:
    """SHA-256 hex digest over a canonical JSON serialization (sorted keys,
    fixed separators, stable type coercion) — NEVER Python's built-in
    hash(), which is not reproducible across processes (the same class of
    defect already found and corrected in this project's Phase 5/8
    history). Same logical input always produces the same digest,
    supporting audit/reproducibility."""
    canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_issue_context(*, issue, column, validation_failure, rule, rule_version) -> dict[str, Any]:
    """Context for an ISSUE-scoped suggestion (EXPLANATION, CORRECTION).
    Metadata only — original_value is issue metadata about WHAT failed
    (already Phase 5/6-visible to any reviewer), never a source row, never
    a credential."""
    return {
        "issue": {
            "severity": issue.severity,
            "status": issue.status,
            "column_name": column.name if column is not None else None,
            "normalized_data_type": column.normalized_data_type if column is not None else None,
        },
        "rule": {
            "rule_type": rule.rule_type,
            "category": rule.category,
            "definition": rule_version.definition,
            "severity": rule_version.severity,
        },
        "validation_failure": {
            "severity": validation_failure.severity,
            "reason": validation_failure.reason,
        },
    }


def _profile_summary(column_profile) -> dict[str, Any] | None:
    """Aggregate statistics only — never value_distribution (a real top-N
    values list) or min_value/max_value as raw strings beyond what a
    correction genuinely needs (min/max ARE included here, unlike the
    rule-recommendation context, because a RANGE/COMPLETENESS correction
    can directly use them as candidate values, and they're already visible
    to any reviewer via the dataset's own profiling view)."""
    if column_profile is None:
        return None
    return {
        "null_percentage": float(column_profile.null_percentage) if column_profile.null_percentage is not None else None,
        "distinct_percentage": (
            float(column_profile.distinct_percentage) if column_profile.distinct_percentage is not None else None
        ),
        "duplicate_percentage": (
            float(column_profile.duplicate_percentage) if column_profile.duplicate_percentage is not None else None
        ),
        "mode_value": column_profile.mode_value,
        "median_value": str(column_profile.median_value) if column_profile.median_value is not None else None,
        "mean_value": str(column_profile.mean_value) if column_profile.mean_value is not None else None,
        "stddev_value": str(column_profile.stddev_value) if column_profile.stddev_value is not None else None,
        "min_value": column_profile.min_value,
        "max_value": column_profile.max_value,
    }


def build_correction_context(
    *, issue, column, validation_failure, rule, rule_version, column_profile, duplicate_examples: list[dict[str, Any]],
    relationship_evidence: dict[str, Any] | None = None, advanced_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Context for a CORRECTION suggestion — the enriched boundary described
    in this module's docstring. duplicate_examples is only ever populated
    for UNIQUENESS/DUPLICATE rule types: other rows' record_ref/row_index
    that share this exact failed_value in the same validation run (never
    their other column values) — enough for the model to say "rows X and Y
    share this value, review which is authoritative" without inventing a
    replacement and without exposing full row content.

    relationship_evidence (Phase 1, additive, optional — omitted entirely
    when None so every pre-existing caller's output is byte-for-byte
    unchanged): an aggregate summary produced by
    AISuggestionService._gather_relationship_evidence(), itself backed by
    app.modules.ai.evidence.discover_relationship_evidence(). Contains only
    statistics (group size, fit quality, a candidate value already computed
    deterministically in Python) — never raw comparable rows or any other
    column's actual values beyond what's already aggregated. The LLM may
    only adopt or decline this candidate, never compute its own.

    advanced_evidence (Phase 4.5, additive, optional, mutually exclusive
    with relationship_evidence in practice — AISuggestionService only ever
    populates one or the other per issue, depending on
    settings.AI_CORRECTION_ADVANCED_INFERENCE_ENABLED): the aggregate
    output of app.modules.ai.candidates.aggregate_candidates() over
    whichever of the four Phase 4 evidence engines (relationship, sequence,
    template, temporal) were applicable to this issue's column/rule_type.
    Same privacy boundary as relationship_evidence — aggregate statistics
    and a single recommended candidate value only, never raw comparable
    rows/pairs or any other column's actual values."""
    context: dict[str, Any] = {
        "issue": {
            "severity": issue.severity,
            "status": issue.status,
            "column_name": column.name if column is not None else None,
            "normalized_data_type": column.normalized_data_type if column is not None else None,
        },
        "rule": {
            "rule_type": rule.rule_type,
            "category": rule.category,
            "definition": rule_version.definition,
            "severity": rule_version.severity,
            "origin": rule.origin,
        },
        "validation_failure": {
            "severity": validation_failure.severity,
            "reason": validation_failure.reason,
            "failed_value": validation_failure.failed_value,
            "expected_value": validation_failure.expected_value,
        },
        "column_profile": _profile_summary(column_profile),
        "duplicate_examples": duplicate_examples,
    }
    if relationship_evidence is not None:
        context["relationship_evidence"] = relationship_evidence
    if advanced_evidence is not None:
        context["advanced_evidence"] = advanced_evidence
    return context


def build_run_summary_context(*, validation_run, dataset, failure_counts_by_severity: dict[str, int]) -> dict[str, Any]:
    """Context for a VALIDATION_RUN-scoped RUN_SUMMARY suggestion —
    aggregated statistics only, never individual failed_value content."""
    return {
        "validation_run": {
            "status": validation_run.status,
            "total_rows": validation_run.total_rows,
            "passed_rows": validation_run.passed_rows,
            "warning_rows": validation_run.warning_rows,
            "failed_rows": validation_run.failed_rows,
            "quality_score": str(validation_run.quality_score) if validation_run.quality_score is not None else None,
        },
        "dataset": {"name": dataset.name, "column_count": dataset.column_count},
        "failure_counts_by_severity": failure_counts_by_severity,
    }


def build_review_run_context(*, review_run, issue_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Context for a REVIEW_RUN-scoped PRIORITIZATION/CLUSTER suggestion —
    per-issue metadata summaries only, never original_value/final_value
    content."""
    return {
        "review_run": {"status": review_run.status},
        "issues": issue_summaries,
    }


# The six rule types approved for Phase 5 — duplicated here rather than
# imported from app.modules.rules.service to keep this module (already
# imported by every suggestion path) free of a dependency on the rules
# module. Frozen at Phase 5; if it ever changes, both copies need updating.
_RULE_TYPES_FOR_PROMPT = ("COMPLETENESS", "UNIQUENESS", "DUPLICATE", "RANGE", "PATTERN", "CROSS_COLUMN")


def build_rule_recommendation_context(*, dataset, columns_with_profiles: list[tuple[Any, Any]]) -> dict[str, Any]:
    """Context for a DATASET-scoped RULE_RECOMMENDATION suggestion covering
    several columns in one batched call. Statistics only, same "metadata,
    never row content" boundary as every context builder in this module —
    deliberately excludes column_profiles.min_value/max_value/mode_value/
    value_distribution even though they're already computed and stored:
    those fields hold real, un-redacted values copied from the source
    data (a literal most-common value, a literal top-10 list), which is
    row content by this boundary's own definition, not metadata about the
    column's shape. RuleDetectionService's own pattern-matching half (no
    LLM, nothing leaves this server) does use those fields — just not
    this function, which builds what actually gets sent to the provider."""
    columns = []
    for column, profile in columns_with_profiles:
        entry: dict[str, Any] = {
            "column_name": column.name,
            "native_data_type": column.native_data_type,
            "normalized_data_type": column.normalized_data_type,
            "is_nullable": column.is_nullable,
        }
        if profile is not None:
            entry["null_percentage"] = float(profile.null_percentage) if profile.null_percentage is not None else None
            entry["distinct_percentage"] = (
                float(profile.distinct_percentage) if profile.distinct_percentage is not None else None
            )
            entry["duplicate_percentage"] = (
                float(profile.duplicate_percentage) if profile.duplicate_percentage is not None else None
            )
            entry["min_length"] = profile.min_length
            entry["max_length"] = profile.max_length
            entry["avg_length"] = float(profile.avg_length) if profile.avg_length is not None else None
        columns.append(entry)

    return {
        "dataset": {"name": dataset.name, "row_count_estimate": dataset.row_count_estimate},
        "columns": columns,
        "supported_rule_types": list(_RULE_TYPES_FOR_PROMPT),
    }

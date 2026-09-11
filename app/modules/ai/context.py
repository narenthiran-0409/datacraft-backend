"""Context assembly + deterministic input-context hashing.

Input boundary (locked Phase 12 decision): allowed context is metadata
only — validation rule definitions/metadata, validation failure metadata,
column metadata, profiling statistics, dataset metadata, review issue
metadata, correction context, aggregated quality statistics. Never sent
to the provider: full raw database rows, passwords, credentials,
connection strings, API keys, secrets. The functions below only ever
read from metadata-shaped columns (name, type, statistics, counts) —
never row_snapshot, failed_value, original_value, final_value,
corrected_fields, or credential_ref.
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

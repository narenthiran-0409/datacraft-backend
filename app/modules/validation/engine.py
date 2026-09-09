"""Rule-type-aware validation engine. Operates only on a pulled row set
(list[dict]) and the six approved rule types — no provider-specific code
belongs here (mirrors app.modules.profiling.engine's separation).

Evaluation mode per rule type (per the approved Phase 5 plan):
  - COMPLETENESS, UNIQUENESS: exact, push-down-informed. The authoritative
    pass/fail signal for the rule as a whole comes from an exact SQL
    aggregate (ColumnExactStats, via get_dataset_column_stats), passed in
    separately from the pulled rows. Per-row failure attribution is derived
    by inspecting the pulled row set, which the caller is responsible for
    making a full scan (not a sample) whenever exactness matters.
  - DUPLICATE, CROSS_COLUMN: exact, in-process, over a full-scan pull. No
    push-down aggregate exists for either in this phase.
  - RANGE, PATTERN: sampled, in-process. Semantically safe under sampling —
    the caller may pass either a sample or a full pull.

REFERENTIAL_INTEGRITY is not implemented: absent from _EVALUATORS_BY_RULE_TYPE
below and from app.modules.rules.service.SUPPORTED_RULE_TYPES. Adding a rule
type later means adding one dict entry and one evaluator function — the
dispatch logic itself never changes.
"""
import re
from dataclasses import dataclass
from typing import Any, Callable

from app.source_adapters.base import ColumnExactStats

SUPPORTED_RULE_TYPES = frozenset({"COMPLETENESS", "UNIQUENESS", "DUPLICATE", "RANGE", "PATTERN", "CROSS_COLUMN"})

# Modes are documented for callers/tests; the engine itself dispatches by
# rule_type alone via _EVALUATORS_BY_RULE_TYPE.
EVALUATION_MODE_BY_RULE_TYPE = {
    "COMPLETENESS": "PUSHDOWN",
    "UNIQUENESS": "PUSHDOWN",
    "DUPLICATE": "IN_PROCESS_EXACT",
    "CROSS_COLUMN": "IN_PROCESS_EXACT",
    "RANGE": "SAMPLED",
    "PATTERN": "SAMPLED",
}


@dataclass(frozen=True)
class RowFailure:
    reason: str
    failed_value: str | None
    expected_value: str | None


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return str(value)
    return value


def evaluate_completeness(
    *, rows: list[dict[str, Any]], column_name: str, definition: dict, exact_stats: ColumnExactStats | None
) -> dict[int, RowFailure]:
    max_null_percentage = float(definition.get("max_null_percentage", 0.0))
    if exact_stats is not None and exact_stats.total_row_count:
        null_percentage = 100.0 * exact_stats.null_count / exact_stats.total_row_count
        if null_percentage <= max_null_percentage:
            return {}
    failures: dict[int, RowFailure] = {}
    for idx, row in enumerate(rows):
        if row.get(column_name) is None:
            failures[idx] = RowFailure(
                reason=f"Column '{column_name}' is null (max_null_percentage={max_null_percentage})",
                failed_value=None,
                expected_value="NOT NULL",
            )
    return failures


def evaluate_uniqueness(
    *, rows: list[dict[str, Any]], column_name: str, definition: dict, exact_stats: ColumnExactStats | None
) -> dict[int, RowFailure]:
    max_duplicate_percentage = float(definition.get("max_duplicate_percentage", 0.0))
    if exact_stats is not None and exact_stats.total_row_count:
        non_null = exact_stats.total_row_count - exact_stats.null_count
        duplicate_count = max(0, non_null - exact_stats.distinct_count)
        duplicate_percentage = 100.0 * duplicate_count / exact_stats.total_row_count
        if duplicate_percentage <= max_duplicate_percentage:
            return {}

    seen: dict[Any, list[int]] = {}
    for idx, row in enumerate(rows):
        value = row.get(column_name)
        if value is None:
            continue
        seen.setdefault(_hashable(value), []).append(idx)

    failures: dict[int, RowFailure] = {}
    for value, indices in seen.items():
        if len(indices) > 1:
            for idx in indices:
                failures[idx] = RowFailure(
                    reason=f"Duplicate value in column '{column_name}'",
                    failed_value=str(value),
                    expected_value="UNIQUE",
                )
    return failures


def evaluate_duplicate(*, rows: list[dict[str, Any]], definition: dict) -> dict[int, RowFailure]:
    """Dataset-level, full-row duplicate detection. Exact — requires the
    caller to have pulled every row, not a sample."""
    seen: dict[tuple, list[int]] = {}
    for idx, row in enumerate(rows):
        key = tuple(sorted((k, _hashable(v)) for k, v in row.items()))
        seen.setdefault(key, []).append(idx)

    failures: dict[int, RowFailure] = {}
    for _key, indices in seen.items():
        if len(indices) > 1:
            for idx in indices:
                failures[idx] = RowFailure(
                    reason="Full-row duplicate", failed_value=None, expected_value="UNIQUE row"
                )
    return failures


def evaluate_range(*, rows: list[dict[str, Any]], column_name: str, definition: dict) -> dict[int, RowFailure]:
    min_value = definition.get("min")
    max_value = definition.get("max")
    failures: dict[int, RowFailure] = {}
    for idx, row in enumerate(rows):
        value = row.get(column_name)
        if value is None:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if (min_value is not None and numeric < float(min_value)) or (
            max_value is not None and numeric > float(max_value)
        ):
            failures[idx] = RowFailure(
                reason=f"Value outside range [{min_value}, {max_value}]",
                failed_value=str(value),
                expected_value=f"[{min_value}, {max_value}]",
            )
    return failures


def evaluate_pattern(*, rows: list[dict[str, Any]], column_name: str, definition: dict) -> dict[int, RowFailure]:
    pattern = definition.get("regex")
    if not pattern:
        return {}
    compiled = re.compile(pattern)
    failures: dict[int, RowFailure] = {}
    for idx, row in enumerate(rows):
        value = row.get(column_name)
        if value is None:
            continue
        if not compiled.match(str(value)):
            failures[idx] = RowFailure(
                reason=f"Value does not match pattern '{pattern}'",
                failed_value=str(value),
                expected_value=pattern,
            )
    return failures


def evaluate_cross_column(
    *, rows: list[dict[str, Any]], column_names_in_order: list[str], definition: dict
) -> dict[int, RowFailure]:
    """Exact, in-process. Supports one check type in Phase 5 (`all_equal`) —
    every listed column must hold an equal value for the row to pass.
    Additional checks can be added later by extending this function's
    dispatch on definition["check"] without touching the engine's rule-type
    registry."""
    check = definition.get("check", "all_equal")
    failures: dict[int, RowFailure] = {}

    if check == "all_equal":
        for idx, row in enumerate(rows):
            values = [row.get(name) for name in column_names_in_order]
            if len(set(_hashable(v) for v in values)) > 1:
                failures[idx] = RowFailure(
                    reason=f"Columns {column_names_in_order} are not all equal",
                    failed_value=str(values),
                    expected_value="all equal",
                )
    return failures


_EVALUATORS_BY_RULE_TYPE: dict[str, Callable] = {
    "COMPLETENESS": evaluate_completeness,
    "UNIQUENESS": evaluate_uniqueness,
    "DUPLICATE": evaluate_duplicate,
    "RANGE": evaluate_range,
    "PATTERN": evaluate_pattern,
    "CROSS_COLUMN": evaluate_cross_column,
}


def is_supported_rule_type(rule_type: str) -> bool:
    return rule_type in _EVALUATORS_BY_RULE_TYPE


def get_evaluator(rule_type: str) -> Callable:
    return _EVALUATORS_BY_RULE_TYPE[rule_type]

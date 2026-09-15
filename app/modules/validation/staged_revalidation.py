"""Phase 4.8 — pure staged-revalidation evaluation.

Reuses the EXACT same rule-type dispatch and evaluator functions
app.modules.validation.tasks uses for a real validation run
(app.modules.validation.engine's get_evaluator/is_supported_rule_type) —
applied to exactly ONE staged row snapshot (a plain dict, already
resolved by StagingService — no DB/provider access happens here). No
second rule language, no duplicated PATTERN/RANGE/COMPLETENESS logic.

Row-local vs cross-row classification — why UNIQUENESS/DUPLICATE cannot
be answered here: evaluate_uniqueness/evaluate_duplicate detect a value
or row that repeats ACROSS OTHER rows in the dataset. Calling either
with rows=[row_snapshot] alone would trivially "pass" every single time
(one row can never collide with itself) — a false, silently misleading
PASS, not a real proof against the dataset's actual population. These
must be classified REQUIRES_DATASET_REVALIDATION rather than executed at
all; a real answer needs a genuine dataset-scope validation run, which
this module deliberately does not attempt.

CROSS_COLUMN is row-local despite its name: app.modules.validation.
engine.evaluate_cross_column's only implemented check (all_equal)
compares several columns of the SAME row against each other — no other
row is ever consulted, so it evaluates correctly here.
"""
from dataclasses import dataclass
from typing import Any

from app.modules.validation.engine import get_evaluator, is_supported_rule_type

ROW_LOCAL_RULE_TYPES = frozenset({"COMPLETENESS", "RANGE", "PATTERN", "CROSS_COLUMN"})
CROSS_ROW_RULE_TYPES = frozenset({"UNIQUENESS", "DUPLICATE"})

STATUS_REVALIDATED_PASS = "REVALIDATED_PASS"
STATUS_REVALIDATED_FAIL = "REVALIDATED_FAIL"
STATUS_REQUIRES_DATASET_REVALIDATION = "REQUIRES_DATASET_REVALIDATION"


@dataclass(frozen=True)
class StagedRuleRevalidation:
    rule_type: str
    column_name: str | None
    status: str
    reason: str | None
    checked_value: Any


def revalidate_row_against_rule(
    *,
    row_snapshot: dict[str, Any],
    rule_type: str,
    definition: dict,
    column_name: str | None = None,
    column_names_in_order: list[str] | None = None,
) -> StagedRuleRevalidation:
    """Never raises for an unsupported/cross-row rule type — degrades to
    an explicit REQUIRES_DATASET_REVALIDATION result instead, exactly
    like the rest of this project's "never fabricate, prefer an explicit
    unavailable state" convention."""
    if rule_type in CROSS_ROW_RULE_TYPES:
        return StagedRuleRevalidation(
            rule_type=rule_type,
            column_name=column_name,
            status=STATUS_REQUIRES_DATASET_REVALIDATION,
            reason=(
                f"{rule_type} cannot be safely proven from a single staged row — it depends on the "
                "dataset's other rows, which staged revalidation deliberately never queries"
            ),
            checked_value=row_snapshot.get(column_name) if column_name else None,
        )
    if rule_type not in ROW_LOCAL_RULE_TYPES or not is_supported_rule_type(rule_type):
        return StagedRuleRevalidation(
            rule_type=rule_type,
            column_name=column_name,
            status=STATUS_REQUIRES_DATASET_REVALIDATION,
            reason=f"rule type {rule_type!r} is not supported for row-local staged revalidation",
            checked_value=None,
        )

    evaluator = get_evaluator(rule_type)
    if rule_type == "COMPLETENESS":
        failures = evaluator(rows=[row_snapshot], column_name=column_name, definition=definition, exact_stats=None)
    elif rule_type == "CROSS_COLUMN":
        failures = evaluator(
            rows=[row_snapshot], column_names_in_order=column_names_in_order or [], definition=definition
        )
    else:  # RANGE, PATTERN
        failures = evaluator(rows=[row_snapshot], column_name=column_name, definition=definition)

    checked_value = row_snapshot.get(column_name) if column_name else None
    failure = failures.get(0)
    if failure is None:
        return StagedRuleRevalidation(
            rule_type=rule_type, column_name=column_name, status=STATUS_REVALIDATED_PASS, reason=None,
            checked_value=checked_value,
        )
    return StagedRuleRevalidation(
        rule_type=rule_type, column_name=column_name, status=STATUS_REVALIDATED_FAIL, reason=failure.reason,
        checked_value=failure.failed_value,
    )

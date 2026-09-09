"""Pure staging record-construction logic — no DB access, unit-testable in
isolation. StagingService calls these while orchestrating the DB/provider
work. Mirrors the state_machine.py/status_logic.py precedent from Phases
6/7.

DRIFT DETECTION (corrected design — see Phase 8 final report): narrowed to
CORRECTED-FIELD-LEVEL comparison, never whole-row hash comparison against
validation_results.source_row_hash. That historical hash was found to be
non-reproducible — not because Phase 5 uses an unstable hash algorithm
(it uses hashlib.sha256, a real cryptographic hash), but because the
column ORDER fed into it comes from an ORDER BY-less query against the
shared `columns` table, which Postgres does not guarantee stable across
time (confirmed via live instrumentation: the same unmodified dataset
produced two different orderings on two separate queries). source_row_hash
is still copied verbatim into source_row_hash_at_validation for
historical/audit completeness, but is never used as a drift-comparison
baseline.

source_row_hash_at_staging is computed by compute_staging_row_hash below —
a Phase-8-owned, genuinely reproducible method (sorted dict keys, no
dependency on any external table's scan order). This makes
Phase-8-to-Phase-8 comparisons (re-staging the same review run) reliable,
even though Phase-5-to-Phase-8 comparisons are not and are never attempted.
"""
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

_NULL_SENTINEL = "\x00NULL\x00"
_UNIT_SEPARATOR = "\x1f"


class StagingIntegrityViolationError(Exception):
    """Raised when an approved issue has no corresponding corrections row,
    or that row has a NULL final_value. Structurally impossible given
    Phase 7's own precondition, but checked here as defense-in-depth —
    caught by the caller and converted into a whole-run FAILED status,
    never silently skipped."""


@dataclass(frozen=True)
class ScopeItem:
    issue_id: Any
    column_id: Any | None
    original_value: str | None
    correction_final_value: str | None  # None means "no correction row or NULL final_value"


def group_by_record_ref(
    rows: list[tuple[str, ScopeItem]],
) -> dict[str, list[ScopeItem]]:
    """rows: (record_ref, ScopeItem) pairs. Groups so that N approved
    issues sharing the same record_ref produce exactly one group — the
    caller turns each group into exactly one staging_records row,
    avoiding a UNIQUE(staging_run_id, record_ref) violation."""
    groups: dict[str, list[ScopeItem]] = defaultdict(list)
    for record_ref, item in rows:
        groups[record_ref].append(item)
    return dict(groups)


def parse_record_ref_to_key_dict(
    record_ref: str, *, key_strategy: str, key_column_names: list[str]
) -> dict[str, Any] | None:
    """Reconstructs {key_column_name: value} from a stored record_ref,
    using the exact inverse of app.modules.validation.record_ref's
    encoding. Returns None when the record cannot be reliably re-fetched
    by key (ROW_INDEX_FALLBACK — no configured key, source row order is
    not guaranteed stable across runs, per Phase 5's own documented
    limitation). Unaffected by the hash non-reproducibility issue: this
    reconstructs key VALUES from record_ref text, not column ordering."""
    if key_strategy == "ROW_INDEX_FALLBACK" or not key_column_names:
        return None

    if key_strategy == "SINGLE_COLUMN":
        value = None if record_ref == _NULL_SENTINEL else record_ref
        return {key_column_names[0]: value}

    # COMPOSITE
    parts = record_ref.split(_UNIT_SEPARATOR)
    return {name: (None if part == _NULL_SENTINEL else part) for name, part in zip(key_column_names, parts)}


def json_safe(value: Any) -> Any:
    """Sanitizes a raw driver value into something JSON-serializable for
    storage in a JSONB column — genuine JSON null, never the string
    "null"."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return value


def compute_staging_row_hash(row: dict[str, Any]) -> str:
    """Phase-8-owned, genuinely reproducible row hash — SHA-256 over a
    canonically serialized row with keys sorted alphabetically (never
    dependent on any table's scan order, unlike Phase 5's
    compute_source_row_hash). Two calls against the identical row content
    always produce the identical hash, regardless of when or in what
    process they run — this is what makes a LATER re-stage's freshly
    computed hash comparable to an EARLIER attempt's
    source_row_hash_at_staging (both computed by this same method)."""
    canonical_items = {k: json_safe(v) for k, v in sorted(row.items())}
    canonical = json.dumps(canonical_items, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_row_snapshot(fetched_row: dict[str, Any] | None, corrected_fields: list[dict]) -> dict[str, Any]:
    """Overlays approved final_value(s) on top of the freshly-fetched
    source row. Every field reflects the source as it is right now,
    regardless of that field's drift status. When the record was not
    found in the source (RECORD_NOT_FOUND), the only data available is
    the corrected fields themselves — row_snapshot degrades to just those,
    still a genuine JSON object."""
    snapshot = {k: json_safe(v) for k, v in fetched_row.items()} if fetched_row is not None else {}
    for field in corrected_fields:
        snapshot[field["column_name"]] = field["final_value"]
    return snapshot


def build_corrected_fields(items: list[ScopeItem], column_name_by_id: dict) -> list[dict]:
    """Raises StagingIntegrityViolationError if any item has no valid
    correction — never silently skips an integrity violation."""
    fields = []
    for item in items:
        if item.correction_final_value is None:
            raise StagingIntegrityViolationError(
                f"Issue {item.issue_id} has no corresponding corrections row with a non-null final_value"
            )
        column_name = column_name_by_id.get(item.column_id)
        fields.append(
            {
                "column_name": column_name,
                "original_value": item.original_value,
                "final_value": item.correction_final_value,
                "issue_id": str(item.issue_id),
            }
        )
    return fields


def classify_drift(
    *, fetched_row_found: bool, corrected_fields: list[dict], fetched_row: dict[str, Any] | None
) -> tuple[str, list[str] | None]:
    """Corrected-field-level comparison ONLY — never a whole-row hash
    comparison (source_row_hash_at_staging is never consulted here). For
    each corrected field, compares its original_value (captured back in
    Phase 6) against the CURRENT freshly-fetched value for that same
    column. An uncorrected column changing is never detected by this
    function — a deliberate, approved narrowing (locked decision 9):
    row_snapshot is still always current for every field regardless of
    what this function returns; only early-warning visibility on
    unrelated columns is reduced."""
    if not fetched_row_found:
        return "RECORD_NOT_FOUND", None

    drifted_columns = []
    for field in corrected_fields:
        column_name = field["column_name"]
        if column_name is None:
            continue
        current_value = fetched_row.get(column_name)
        # original_value (issues.original_value, TEXT) was captured via
        # str(value) at validation time (see
        # app.modules.validation.engine's failure recording) — stringify
        # the freshly-fetched value the same way for a like-for-like compare.
        current_as_str = None if current_value is None else str(current_value)
        if current_as_str != field["original_value"]:
            drifted_columns.append(column_name)

    if drifted_columns:
        return "VALUE_CHANGED", drifted_columns
    return "UNCHANGED", None

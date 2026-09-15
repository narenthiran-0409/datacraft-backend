"""Phase 4.6 — pure business-key candidate discovery.

Deterministic, provider-agnostic, no DB/SQLAlchemy/FastAPI/provider
imports — operates entirely over an already-fetched row set and column
metadata, mirroring the architectural discipline of the Phase 4 evidence
engines (app.modules.ai.evidence*.py): no LLM, no business-column-name
assumptions, unit-testable in complete isolation.

DETECTION != ADOPTION. This module only ever discovers and ranks
candidates — it never writes dataset_key_columns or datasets.key_strategy.
That mutation lives exclusively in
app.modules.datasets.key_resolution.apply_key_columns, invoked only by the
separate, explicit confirmation service
(app.modules.datasets.business_key_service.BusinessKeyService.confirm).

Uniqueness bar (deliberately exact, never percentage-based): a candidate
column set passes only when, across every row actually evaluated, no row
has a NULL in any component AND every combination of values is distinct.
99%-unique is not "nearly a key" here — it is simply not a candidate.

Sample vs. verified: the caller tells this module whether the row set it
fetched is the complete source table (is_full_scan=True, from
SourceDatabaseProvider.sample_rows()'s own documented full-scan
guarantee) or a bounded sample. A candidate computed from a bounded
sample can only ever be SAMPLE_CANDIDATE, never VERIFIED_UNIQUE —
regardless of how clean the sample looks.

Search order: single columns first; only if zero single-column candidates
pass does the search widen to 2-column combinations, then (only if that
also yields zero) 3-column combinations. This ordering is what enforces
minimality — a width is only ever searched once the narrower width has
been proven to contain no candidate at all, so a wider combination is
never recommended when a smaller one already proves uniqueness.
"""
from dataclasses import dataclass
from itertools import combinations
from typing import Any

# Free-form/unbounded (TEXT), boolean (never usably unique beyond a
# couple of rows), and approximate/floating numeric (DECIMAL, which this
# codebase's normalized types conflate with true floating point — see
# postgresql_provider._TYPE_MAP mapping real/double precision to DECIMAL)
# are excluded outright. This is a type-based exclusion, never a column
# NAME-based one.
_ELIGIBLE_TYPES = frozenset({"STRING", "INTEGER", "DATE", "DATETIME"})
_MAX_COMPOSITE_WIDTH = 3
_MIN_OBSERVATIONS = 2

STATUS_VERIFIED_UNIQUE = "VERIFIED_UNIQUE"
STATUS_SAMPLE_CANDIDATE = "SAMPLE_CANDIDATE"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_NO_CANDIDATE = "NO_CANDIDATE"


@dataclass(frozen=True)
class ColumnMeta:
    name: str
    normalized_data_type: str


@dataclass(frozen=True)
class CandidateEvidence:
    columns: tuple[str, ...]
    width: int
    status: str  # "CANDIDATE" | "REJECTED"
    reason: str | None
    total_rows_evaluated: int
    null_key_rows: int
    distinct_key_count: int
    duplicate_key_groups: int
    verification_level: str | None  # "VERIFIED_UNIQUE" | "SAMPLE_CANDIDATE" | None when REJECTED


@dataclass(frozen=True)
class RejectedColumn:
    name: str
    normalized_data_type: str
    reason: str


@dataclass(frozen=True)
class BusinessKeyDiscoveryResult:
    status: str  # VERIFIED_UNIQUE | SAMPLE_CANDIDATE | AMBIGUOUS | NO_CANDIDATE
    recommended: CandidateEvidence | None
    candidates: tuple[CandidateEvidence, ...]
    evaluated: tuple[CandidateEvidence, ...]
    rejected_columns: tuple[RejectedColumn, ...]
    widths_searched: tuple[int, ...]
    total_rows_evaluated: int
    is_full_scan: bool
    reason: str | None = None


def _evaluate_combo(
    combo_columns: tuple[str, ...], rows: list[dict[str, Any]], is_full_scan: bool
) -> CandidateEvidence:
    total = len(rows)
    null_key_rows = 0
    seen: dict[tuple, int] = {}
    for row in rows:
        values = tuple(row.get(c) for c in combo_columns)
        if any(v is None for v in values):
            null_key_rows += 1
            continue
        seen[values] = seen.get(values, 0) + 1

    distinct_key_count = len(seen)
    duplicate_key_groups = sum(1 for count in seen.values() if count > 1)
    passes = null_key_rows == 0 and duplicate_key_groups == 0 and distinct_key_count == total and total > 0

    if passes:
        return CandidateEvidence(
            columns=combo_columns, width=len(combo_columns), status="CANDIDATE", reason=None,
            total_rows_evaluated=total, null_key_rows=null_key_rows, distinct_key_count=distinct_key_count,
            duplicate_key_groups=duplicate_key_groups,
            verification_level=STATUS_VERIFIED_UNIQUE if is_full_scan else STATUS_SAMPLE_CANDIDATE,
        )

    if null_key_rows > 0 and duplicate_key_groups > 0:
        reason = "contains_null_values_and_duplicate_values"
    elif null_key_rows > 0:
        reason = "contains_null_values"
    elif duplicate_key_groups > 0:
        reason = "duplicate_values"
    else:
        reason = "no_rows_evaluated"
    return CandidateEvidence(
        columns=combo_columns, width=len(combo_columns), status="REJECTED", reason=reason,
        total_rows_evaluated=total, null_key_rows=null_key_rows, distinct_key_count=distinct_key_count,
        duplicate_key_groups=duplicate_key_groups, verification_level=None,
    )


def _display_sort_key(evidence: CandidateEvidence) -> tuple:
    # Deterministic DISPLAY ordering only — never used to break a genuine
    # tie into a false single winner. Ambiguity is decided before this is
    # ever applied.
    return (evidence.width, evidence.columns)


def discover_business_key_candidates(
    *, columns: list[ColumnMeta], rows: list[dict[str, Any]], is_full_scan: bool
) -> BusinessKeyDiscoveryResult:
    """Deterministic candidate discovery over an already-fetched row set.

    columns: metadata for every column to consider (callers should pass
    every active column of the dataset; type-based exclusion happens
    here, not by the caller pre-filtering).
    rows: the fetched row set — either the complete source table
    (is_full_scan=True) or a bounded sample (is_full_scan=False).
    """
    total_rows = len(rows)
    eligible = [c for c in columns if (c.normalized_data_type or "").upper() in _ELIGIBLE_TYPES]
    rejected_columns = tuple(
        RejectedColumn(name=c.name, normalized_data_type=c.normalized_data_type, reason="unsupported_type")
        for c in columns
        if (c.normalized_data_type or "").upper() not in _ELIGIBLE_TYPES
    )

    if total_rows < _MIN_OBSERVATIONS:
        return BusinessKeyDiscoveryResult(
            status=STATUS_NO_CANDIDATE, recommended=None, candidates=(), evaluated=(),
            rejected_columns=rejected_columns, widths_searched=(), total_rows_evaluated=total_rows,
            is_full_scan=is_full_scan, reason="insufficient_row_count",
        )

    if not eligible:
        return BusinessKeyDiscoveryResult(
            status=STATUS_NO_CANDIDATE, recommended=None, candidates=(), evaluated=(),
            rejected_columns=rejected_columns, widths_searched=(), total_rows_evaluated=total_rows,
            is_full_scan=is_full_scan, reason="no_eligible_columns",
        )

    eligible_names = [c.name for c in eligible]
    widths_searched: list[int] = []
    all_evaluated: list[CandidateEvidence] = []

    max_width = min(_MAX_COMPOSITE_WIDTH, len(eligible_names))
    for width in range(1, max_width + 1):
        widths_searched.append(width)
        width_evidence = [
            _evaluate_combo(combo, rows, is_full_scan) for combo in combinations(eligible_names, width)
        ]
        all_evaluated.extend(width_evidence)
        passing = [e for e in width_evidence if e.status == "CANDIDATE"]
        if passing:
            ordered = tuple(sorted(passing, key=_display_sort_key))
            if len(ordered) == 1:
                return BusinessKeyDiscoveryResult(
                    status=ordered[0].verification_level, recommended=ordered[0], candidates=ordered,
                    evaluated=tuple(all_evaluated), rejected_columns=rejected_columns,
                    widths_searched=tuple(widths_searched), total_rows_evaluated=total_rows,
                    is_full_scan=is_full_scan,
                )
            return BusinessKeyDiscoveryResult(
                status=STATUS_AMBIGUOUS, recommended=None, candidates=ordered,
                evaluated=tuple(all_evaluated), rejected_columns=rejected_columns,
                widths_searched=tuple(widths_searched), total_rows_evaluated=total_rows,
                is_full_scan=is_full_scan, reason="multiple_equally_minimal_verified_candidates",
            )

    return BusinessKeyDiscoveryResult(
        status=STATUS_NO_CANDIDATE, recommended=None, candidates=(), evaluated=tuple(all_evaluated),
        rejected_columns=rejected_columns, widths_searched=tuple(widths_searched),
        total_rows_evaluated=total_rows, is_full_scan=is_full_scan,
        reason="no_defensible_candidate_up_to_max_width",
    )

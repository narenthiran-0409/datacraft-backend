"""Evidence Engine (Phase 0) — pure, provider-agnostic relationship discovery.

Deliberately independent of everything else in this application: no imports
from app.db.models, app.source_adapters, sqlalchemy, Celery, or any AI
orchestration module. Operates only on in-memory rows (plain dicts) and
lightweight metadata (ColumnMeta) passed in by the caller. A later
integration phase is responsible for gathering candidate_rows (e.g. from a
live source query) and adapting real ORM Column/ColumnProfile objects into
ColumnMeta — none of that belongs here.

Mirrors app.modules.profiling.engine's style: pure functions, no I/O, no
provider awareness, fully deterministic and reproducible for the same
inputs. Nothing in this module ever calls an LLM — every number here is
computed with plain arithmetic/statistics, so hallucination-prevention is
structural (the LLM, in a later phase, can only adopt or decline a
candidate this module already computed — never invent its own).

Column role classification (IDENTIFIER / DIMENSION / MEASURE / OTHER) is
driven ONLY by generic, already-computable signals — data type, whether the
column is a primary key, and value cardinality (distinct_percentage) —
never by column name or any assumption about business meaning. See
classify_column_role() for the one deliberate asymmetry in that logic
(numeric vs. string identifier promotion) and why it matters.

Relationship discovery tests exactly five shapes against a comparable
group (never against the failing row's own target value, which is always
excluded from every statistic):
  1. constant_within_group   — target is ~constant across the group
  2. ratio_consistency       — target / other_measure is ~constant
  3. product_consistency     — target ~= measure_a * measure_b
  4. sum_consistency         — target ~= measure_a + measure_b
  5. difference_consistency  — target ~= measure_a - measure_b (order matters)

A candidate is only ever returned when fit_quality clears a configurable
threshold AND the comparable group meets a configurable minimum size; if
several shapes fit similarly well but disagree on the resulting value, the
result is marked AMBIGUOUS with no chosen candidate — every shape actually
tested (qualifying or not) is preserved in the result for diagnostics.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from itertools import combinations, permutations
from typing import Any, Mapping, Sequence

# --- Column role classification ---------------------------------------------

_NUMERIC_TYPES = frozenset({"INTEGER", "DECIMAL"})
_CATEGORICAL_TYPES = frozenset({"STRING", "TEXT", "BOOLEAN"})

_IDENTIFIER_MIN_DISTINCT_PCT = 95.0
_DIMENSION_MAX_DISTINCT_PCT = 50.0
# Below this sample size, "100% distinct" is expected by chance for a
# continuous numeric measure (e.g. 4 distinct Qty values in a 4-row sample)
# and must NOT be read as identifier-like. String columns get no such gate —
# a categorical dimension almost always repeats even in a tiny sample, so a
# string column that is nonetheless all-unique is a much stronger identifier
# signal regardless of how few rows were sampled.
_MIN_SAMPLE_FOR_NUMERIC_IDENTIFIER_PROMOTION = 20

# Bounds the combinatorial search on wide tables — cheap, deterministic
# (preserves input order), and never silently changes behavior for any
# table encountered in practice (well under this cap).
_MAX_MEASURE_COLUMNS = 15


class ColumnRole(str, Enum):
    IDENTIFIER = "IDENTIFIER"
    DIMENSION = "DIMENSION"
    MEASURE = "MEASURE"
    # Phase 4.1 foundation only: added to the enum so later Phase 4
    # sub-phases (date-progression evidence, richer attribute handling)
    # have a stable name to target. classify_column_role() does NOT return
    # either of these yet — DATE/DATETIME columns still classify as OTHER,
    # exactly as before this change, until a later sub-phase implements
    # real date-progression evidence and deliberately wires this in.
    DATE_TIME = "DATE_TIME"
    ATTRIBUTE = "ATTRIBUTE"
    OTHER = "OTHER"


@dataclass(frozen=True)
class ColumnMeta:
    """The minimal, already-generic metadata this module needs per column.
    A later integration phase adapts real Column/ColumnProfile ORM rows into
    this shape — nothing here assumes where the data came from."""

    name: str
    normalized_data_type: str
    is_primary_key: bool = False
    # 0-100, dataset-wide if known (e.g. from ColumnProfile). Preferred over
    # sample-derived when provided; derived from sample_rows otherwise.
    distinct_percentage: float | None = None


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return str(value)
    return value


def _derive_distinct_percentage(column_name: str, rows: Sequence[Mapping[str, Any]]) -> float | None:
    values = [r.get(column_name) for r in rows if r.get(column_name) is not None]
    if not values:
        return None
    distinct = len({_hashable(v) for v in values})
    return 100.0 * distinct / len(values)


def classify_column_role(
    meta: ColumnMeta, *, sample_rows: Sequence[Mapping[str, Any]] | None = None
) -> ColumnRole:
    """Pure, name-agnostic classification. See module docstring for the
    numeric-vs-string identifier-promotion asymmetry and why it exists."""
    if meta.is_primary_key:
        return ColumnRole.IDENTIFIER

    distinct_pct = meta.distinct_percentage
    sample_size: int | None = len(sample_rows) if sample_rows is not None else None
    if distinct_pct is None and sample_rows is not None:
        distinct_pct = _derive_distinct_percentage(meta.name, sample_rows)

    data_type = (meta.normalized_data_type or "").upper()

    if data_type in _NUMERIC_TYPES:
        if (
            distinct_pct is not None
            and distinct_pct >= _IDENTIFIER_MIN_DISTINCT_PCT
            and sample_size is not None
            and sample_size >= _MIN_SAMPLE_FOR_NUMERIC_IDENTIFIER_PROMOTION
        ):
            return ColumnRole.IDENTIFIER
        return ColumnRole.MEASURE

    if data_type in _CATEGORICAL_TYPES:
        if distinct_pct is not None and distinct_pct >= _IDENTIFIER_MIN_DISTINCT_PCT:
            return ColumnRole.IDENTIFIER
        return ColumnRole.DIMENSION

    # DATE/DATETIME and anything unrecognized: not used for relationship
    # math or grouping in this phase — a documented simplification, not an
    # oversight (dates are rarely useful group keys or arithmetic measures
    # in the same sense; a later phase can extend this deliberately).
    return ColumnRole.OTHER


def classify_columns(
    columns: Sequence[ColumnMeta], *, sample_rows: Sequence[Mapping[str, Any]] | None = None
) -> dict[str, ColumnRole]:
    return {c.name: classify_column_role(c, sample_rows=sample_rows) for c in columns}


# --- Relationship evidence ----------------------------------------------------


@dataclass(frozen=True)
class RelationshipEvidence:
    relationship_type: str
    description: str
    related_columns: tuple[str, ...]
    candidate_value: float
    comparable_group_size: int
    fit_quality: float  # 0-1, pure goodness-of-fit (coefficient-of-variation or mean-relative-error based)
    residual: float  # the underlying error measure fit_quality was derived from
    coefficient_of_variation: float | None  # set for constant/ratio shapes; None for product/sum/difference
    confidence: float  # fit_quality discounted by comparable_group_size — see _confidence()


@dataclass(frozen=True)
class EvidenceResult:
    status: str  # "CANDIDATE" | "NO_RELATIONSHIP" | "AMBIGUOUS" | "INSUFFICIENT_GROUP"
    target_column: str
    comparable_group_size: int
    best: RelationshipEvidence | None
    rejected_alternatives: tuple[RelationshipEvidence, ...]
    reason: str


def _as_float(value: Any) -> float | None:
    """Accepts int/float/Decimal only (never bool, never a numeric-looking
    string) — mirrors profiling.engine's own numeric-value guard. Rejects
    NaN/inf outright, per the safety requirement."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        f = float(value)
        return f if math.isfinite(f) else None
    return None


def _confidence(fit_quality: float, group_size: int, min_group_size: int) -> float:
    """A deliberately more conservative figure than fit_quality alone: a
    group barely at the minimum size gets confidence discounted toward
    half of its raw fit_quality, even if the fit is numerically perfect —
    small-but-perfect groups deserve less trust than large-and-perfect
    ones. Reaches full fit_quality once the group is at least 2x the
    configured minimum."""
    size_factor = min(1.0, group_size / (min_group_size * 2))
    return fit_quality * size_factor


def _fit_constant(values: list[float]) -> tuple[float, float, float] | None:
    mean = statistics.fmean(values)
    if mean == 0:
        return None  # a "constant of zero" is degenerate — nothing to divide by for a CV
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    cv = abs(stdev / mean)
    fit_quality = max(0.0, 1.0 - cv)
    return mean, fit_quality, cv


def _fit_ratio(pairs: list[tuple[float, float]]) -> tuple[float, float, float] | None:
    ratios = [t / o for t, o in pairs if o != 0]
    if not ratios:
        return None
    mean_ratio = statistics.fmean(ratios)
    if mean_ratio == 0:
        return None
    stdev_ratio = statistics.pstdev(ratios) if len(ratios) > 1 else 0.0
    cv = abs(stdev_ratio / mean_ratio)
    fit_quality = max(0.0, 1.0 - cv)
    return mean_ratio, fit_quality, cv


def _fit_derived(pairs: list[tuple[float, float]]) -> tuple[float, float] | None:
    """pairs = [(target, derived), ...]; tests target ≈ derived (the
    coefficient is fixed at 1 — used by product/sum/difference, which have
    no separate scale factor to fit, unlike ratio_consistency)."""
    if not pairs:
        return None
    errors = []
    for t, d in pairs:
        denom = max(abs(t), abs(d), 1e-9)
        errors.append(abs(t - d) / denom)
    mean_err = statistics.fmean(errors)
    fit_quality = max(0.0, 1.0 - mean_err)
    return fit_quality, mean_err


def _numeric_pairs(rows: Sequence[Mapping[str, Any]], col_a: str, col_b: str) -> list[tuple[float, float]]:
    out = []
    for r in rows:
        a = _as_float(r.get(col_a))
        b = _as_float(r.get(col_b))
        if a is not None and b is not None:
            out.append((a, b))
    return out


def discover_relationship_evidence(
    *,
    target_column: str,
    failing_row: Mapping[str, Any],
    candidate_rows: Sequence[Mapping[str, Any]],
    columns: Sequence[ColumnMeta],
    min_group_size: int = 3,
    min_fit_quality: float = 0.9,
    ambiguity_margin: float = 0.03,
) -> EvidenceResult:
    """The one entry point. candidate_rows is the bounded pool a caller has
    already gathered (e.g. a sampled query against the live source) — NOT
    pre-filtered to a comparable group; this function does that filtering
    itself, by classifying columns and matching every DIMENSION column
    against the failing row (falling back to the whole pool when no
    DIMENSION column exists — required for whole-dataset relationships like
    a flat tax rate with no categorical grouping column at all).

    The failing row is excluded from every statistic computed here — its
    own target-column value is never read by this function at all; only
    its OTHER column values are used, and only at the very end, to turn a
    fitted relationship into a candidate_value for the failing row
    specifically.
    """
    roles = classify_columns(columns, sample_rows=candidate_rows)
    dimension_columns = [
        name for name, role in roles.items() if role is ColumnRole.DIMENSION and name != target_column
    ]
    measure_columns = [
        name for name, role in roles.items() if role is ColumnRole.MEASURE and name != target_column
    ][:_MAX_MEASURE_COLUMNS]

    # Never let the failing row influence its own replacement's evidence.
    pool = [r for r in candidate_rows if r != failing_row]

    if dimension_columns:
        group = [r for r in pool if all(r.get(d) == failing_row.get(d) for d in dimension_columns)]
        grouping_desc = f"matching {', '.join(dimension_columns)}"
    else:
        group = pool
        grouping_desc = "the whole comparable dataset (no dimension column found)"

    if len(group) < min_group_size:
        return EvidenceResult(
            status="INSUFFICIENT_GROUP",
            target_column=target_column,
            comparable_group_size=len(group),
            best=None,
            rejected_alternatives=(),
            reason=(
                f"Only {len(group)} comparable row(s) found ({grouping_desc}); "
                f"minimum required is {min_group_size}."
            ),
        )

    candidates: list[RelationshipEvidence] = []

    # 1. Constant-within-group
    target_vals = [v for v in (_as_float(r.get(target_column)) for r in group) if v is not None]
    if len(target_vals) >= min_group_size:
        fit = _fit_constant(target_vals)
        if fit is not None:
            candidate_value, fit_quality, cv = fit
            candidates.append(
                RelationshipEvidence(
                    relationship_type="constant_within_group",
                    description=(
                        f"{target_column} is approximately constant ({candidate_value:.4g}) "
                        f"across {len(target_vals)} comparable rows"
                    ),
                    related_columns=(),
                    candidate_value=candidate_value,
                    comparable_group_size=len(target_vals),
                    fit_quality=fit_quality,
                    residual=cv,
                    coefficient_of_variation=cv,
                    confidence=_confidence(fit_quality, len(target_vals), min_group_size),
                )
            )

    # 2. Ratio consistency: target / measure
    for m in measure_columns:
        pairs = [(t, o) for t, o in _numeric_pairs(group, target_column, m) if o != 0]
        if len(pairs) < min_group_size:
            continue
        fit = _fit_ratio(pairs)
        if fit is None:
            continue
        ratio, fit_quality, cv = fit
        failing_m = _as_float(failing_row.get(m))
        if failing_m is None or failing_m == 0:
            continue
        candidate_value = ratio * failing_m
        if not math.isfinite(candidate_value):
            continue
        candidates.append(
            RelationshipEvidence(
                relationship_type="ratio_consistency",
                description=(
                    f"{target_column} / {m} is approximately constant ({ratio:.4g}) "
                    f"across {len(pairs)} comparable rows"
                ),
                related_columns=(m,),
                candidate_value=candidate_value,
                comparable_group_size=len(pairs),
                fit_quality=fit_quality,
                residual=cv,
                coefficient_of_variation=cv,
                confidence=_confidence(fit_quality, len(pairs), min_group_size),
            )
        )

    # 3. Product consistency: target ≈ m1 * m2 (commutative — unordered pairs)
    for m1, m2 in combinations(measure_columns, 2):
        rows_vals = [
            (t, a * b)
            for t, a, b in (
                (_as_float(r.get(target_column)), _as_float(r.get(m1)), _as_float(r.get(m2))) for r in group
            )
            if t is not None and a is not None and b is not None
        ]
        if len(rows_vals) < min_group_size:
            continue
        fit = _fit_derived(rows_vals)
        if fit is None:
            continue
        fit_quality, mean_err = fit
        fa, fb = _as_float(failing_row.get(m1)), _as_float(failing_row.get(m2))
        if fa is None or fb is None:
            continue
        candidate_value = fa * fb
        if not math.isfinite(candidate_value):
            continue
        candidates.append(
            RelationshipEvidence(
                relationship_type="product_consistency",
                description=f"{target_column} is approximately {m1} × {m2} across {len(rows_vals)} comparable rows",
                related_columns=(m1, m2),
                candidate_value=candidate_value,
                comparable_group_size=len(rows_vals),
                fit_quality=fit_quality,
                residual=mean_err,
                coefficient_of_variation=None,
                confidence=_confidence(fit_quality, len(rows_vals), min_group_size),
            )
        )

    # 4. Sum consistency: target ≈ m1 + m2 (commutative — unordered pairs)
    for m1, m2 in combinations(measure_columns, 2):
        rows_vals = [
            (t, a + b)
            for t, a, b in (
                (_as_float(r.get(target_column)), _as_float(r.get(m1)), _as_float(r.get(m2))) for r in group
            )
            if t is not None and a is not None and b is not None
        ]
        if len(rows_vals) < min_group_size:
            continue
        fit = _fit_derived(rows_vals)
        if fit is None:
            continue
        fit_quality, mean_err = fit
        fa, fb = _as_float(failing_row.get(m1)), _as_float(failing_row.get(m2))
        if fa is None or fb is None:
            continue
        candidate_value = fa + fb
        if not math.isfinite(candidate_value):
            continue
        candidates.append(
            RelationshipEvidence(
                relationship_type="sum_consistency",
                description=f"{target_column} is approximately {m1} + {m2} across {len(rows_vals)} comparable rows",
                related_columns=(m1, m2),
                candidate_value=candidate_value,
                comparable_group_size=len(rows_vals),
                fit_quality=fit_quality,
                residual=mean_err,
                coefficient_of_variation=None,
                confidence=_confidence(fit_quality, len(rows_vals), min_group_size),
            )
        )

    # 5. Difference consistency: target ≈ m1 - m2 (NOT commutative — ordered pairs)
    for m1, m2 in permutations(measure_columns, 2):
        rows_vals = [
            (t, a - b)
            for t, a, b in (
                (_as_float(r.get(target_column)), _as_float(r.get(m1)), _as_float(r.get(m2))) for r in group
            )
            if t is not None and a is not None and b is not None
        ]
        if len(rows_vals) < min_group_size:
            continue
        fit = _fit_derived(rows_vals)
        if fit is None:
            continue
        fit_quality, mean_err = fit
        fa, fb = _as_float(failing_row.get(m1)), _as_float(failing_row.get(m2))
        if fa is None or fb is None:
            continue
        candidate_value = fa - fb
        if not math.isfinite(candidate_value):
            continue
        candidates.append(
            RelationshipEvidence(
                relationship_type="difference_consistency",
                description=f"{target_column} is approximately {m1} - {m2} across {len(rows_vals)} comparable rows",
                related_columns=(m1, m2),
                candidate_value=candidate_value,
                comparable_group_size=len(rows_vals),
                fit_quality=fit_quality,
                residual=mean_err,
                coefficient_of_variation=None,
                confidence=_confidence(fit_quality, len(rows_vals), min_group_size),
            )
        )

    qualifying = sorted((c for c in candidates if c.fit_quality >= min_fit_quality), key=lambda c: c.fit_quality, reverse=True)
    all_sorted = tuple(sorted(candidates, key=lambda c: c.fit_quality, reverse=True))

    if not qualifying:
        reason = (
            f"No relationship shape reached the minimum fit quality of {min_fit_quality:.2f} "
            f"({len(candidates)} shape(s) tested)."
            if candidates
            else "No relationship shape had enough clean, non-null comparable data to test."
        )
        return EvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            comparable_group_size=len(group),
            best=None,
            rejected_alternatives=all_sorted,
            reason=reason,
        )

    top = qualifying[0]
    competitors = [
        c
        for c in qualifying[1:]
        if (top.fit_quality - c.fit_quality) <= ambiguity_margin
        and not math.isclose(c.candidate_value, top.candidate_value, rel_tol=0.01, abs_tol=1e-6)
    ]

    if competitors:
        return EvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            comparable_group_size=len(group),
            best=None,
            rejected_alternatives=all_sorted,
            reason=(
                f"{len(competitors) + 1} relationships fit similarly well (within {ambiguity_margin:.2f} "
                "fit quality) but disagree on the resulting value — declining to choose one automatically."
            ),
        )

    rejected = tuple(c for c in all_sorted if c is not top)
    return EvidenceResult(
        status="CANDIDATE",
        target_column=target_column,
        comparable_group_size=len(group),
        best=top,
        rejected_alternatives=rejected,
        reason=f"{top.relationship_type} fit {top.fit_quality:.3f} across {top.comparable_group_size} comparable rows.",
    )

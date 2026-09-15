"""Date/datetime progression Evidence Engine (Phase 4.4) — pure,
provider-agnostic, column-name-agnostic.

Mirrors app.modules.ai.evidence / evidence_sequence / evidence_template's
isolation rule exactly: no imports from app.db.models, app.source_adapters,
sqlalchemy, Celery, or any AI orchestration module. Operates only on a
plain sequence of already-observed temporal values passed in by a caller.
NOT WIRED into suggestion_service.py, candidates.py, or anything else yet
(Phase 4.4 is a pure, standalone foundation, exactly like Phases 0, 4.2,
and 4.3).

ORDERING DECISION (documented per the spec's explicit request): evidence is
always evaluated by chronological sort of the observed VALUES themselves,
never by the row/input order they were supplied in — mirrors
evidence_sequence.py's identical decision for numeric identifiers, for the
identical reason: row order is an artifact of however a caller happened to
gather the pool (e.g. a bounded provider.sample_rows() call), not a
property of the temporal sequence itself. Duplicate handling depends on
this: a duplicate is "the same distinct instant observed more than once",
which set/multiset arithmetic captures whether or not the caller happened
to supply it in sorted order.

STRATEGIES TRIED, IN ORDER
--------------------------
1. FIXED_INTERVAL — a single, consistent timedelta step (whatever it
   actually is: a minute, an hour, a day, seven days, ...). This is
   exactly evidence_sequence.py's numeric algorithm with `int` replaced by
   `datetime`/`timedelta` — see that module's docstring for the identical
   "count, not ratio" reasoning behind _MIN_SUPPORTING_TRANSITIONS.
2. CALENDAR_MONTH_INTERVAL — tried only if (1) did not produce a
   candidate. Detects a consistent N-calendar-month step where every
   observed date independently satisfies ONE of two conventions
   (same day-of-month every time, e.g. the 15th; or every date is the
   last day of its own month) — never a mix of both, and never assumed
   from fewer than min_supporting_transitions dates satisfying it.
3. CALENDAR_YEAR_INTERVAL — same idea, one level up: a consistent
   N-year step with the month-and-day held constant (safely rejecting a
   recurring Feb 29 landing on a non-leap target year rather than
   inventing Feb 28 or Mar 1).

Nothing in this module ever reads target_column for a decision — it is
carried through purely as a result label, exactly like the other three
Phase 4 evidence modules.

ANTI-OVERFITTING / SAFETY
-------------------------
  - No candidate is ever produced from a single transition, a single
    pair, or "only" a min/max reading — every strategy requires an
    ABSOLUTE floor (min_supporting_transitions, default 3) of
    independently confirming evidence, not a ratio (see
    evidence_sequence.py's docstring for why a ratio unfairly penalizes
    the exact single-gap case this module exists to detect).
  - end-of-month / same-day-of-month conventions are required to hold for
    EVERY observed date with zero exceptions before being trusted at all
    — "one date happens to be month-end" is explicitly never sufficient.
  - A wildly distant outlier (e.g. one date decades away from a tight
    cluster) is capped before ever materializing a large expected-range
    set — see _MAX_EXPECTED_RANGE_SLOTS / _MAX_EXPECTED_MONTHS /
    _MAX_EXPECTED_YEARS.
  - Mixed naive/timezone-aware values, or aware values with inconsistent
    UTC offsets, are rejected outright (AMBIGUOUS) rather than silently
    normalized — never invents a timezone assumption.
  - An invalid calendar date (e.g. Feb 29 required for a non-leap target
    year) is never silently substituted with a nearby valid date — the
    strategy declines instead.
"""
from __future__ import annotations

import calendar
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Sequence

_MIN_STEP_CONSISTENCY_FOR_ANY_PATTERN = 0.5
_MIN_SUPPORTING_TRANSITIONS = 3
_MAX_EXPECTED_RANGE_SLOTS = 10_000
_MAX_EXPECTED_MONTHS = 2_000
_MAX_EXPECTED_YEARS = 2_000

# A bare "1/2/2026" or "01-02-26"-shaped string is locale-ambiguous
# (day-first vs. month-first) — rejected outright rather than guessed.
_AMBIGUOUS_SLASH_OR_DASH_DATE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")


@dataclass(frozen=True)
class TemporalEvidence:
    strategy: str  # "TEMPORAL_GAP" | "TEMPORAL_NEXT_VALUE"
    description: str
    candidate_value: str  # ISO 8601 — date-only ("YYYY-MM-DD") or full datetime, matching the observed granularity
    progression_type: str  # "FIXED_INTERVAL" | "CALENDAR_MONTH_INTERVAL" | "CALENDAR_YEAR_INTERVAL"
    interval: str  # human-readable, e.g. "1 day", "7 days", "1 calendar month (end-of-month)", "1 calendar year"
    observed_values_count: int
    duplicate_count: int
    gap_count: int
    supporting_count: int
    contradicting_count: int
    consistency: float
    confidence: float
    candidate_already_exists: bool


@dataclass(frozen=True)
class TemporalEvidenceResult:
    status: str  # "CANDIDATE" | "NO_RELATIONSHIP" | "AMBIGUOUS" | "INSUFFICIENT_GROUP"
    target_column: str
    observed_values_count: int
    best: TemporalEvidence | None
    rejected_alternatives: tuple[TemporalEvidence, ...]
    reason: str


@dataclass(frozen=True)
class _Parsed:
    instant: datetime  # always tz-consistent within one call (see _normalize_all)
    is_date_only: bool


def _looks_ambiguous(text: str) -> bool:
    return bool(_AMBIGUOUS_SLASH_OR_DASH_DATE.match(text))


def _parse_one(value: Any) -> _Parsed | None:
    if isinstance(value, datetime):
        return _Parsed(instant=value, is_date_only=False)
    if isinstance(value, date):
        return _Parsed(instant=datetime.combine(value, time.min), is_date_only=True)
    if isinstance(value, str):
        text = value.strip()
        if not text or _looks_ambiguous(text):
            return None
        try:
            if "T" in text or (" " in text and re.search(r"\d:\d", text)):
                return _Parsed(instant=datetime.fromisoformat(text.replace("Z", "+00:00")), is_date_only=False)
            return _Parsed(instant=datetime.combine(date.fromisoformat(text), time.min), is_date_only=True)
        except ValueError:
            return None
    return None


def _normalize_all(raw_values: Sequence[Any]) -> tuple[list[_Parsed], str | None]:
    """Parses every non-null value, then enforces timezone consistency
    across the WHOLE pool. Returns (parsed, rejection_reason) — parsed is
    empty and rejection_reason is set if the pool mixes naive and aware
    values, or aware values with inconsistent UTC offsets."""
    parsed = [p for p in (_parse_one(v) for v in raw_values if v is not None and str(v).strip() != "") if p is not None]
    if not parsed:
        return [], None

    aware_flags = {p.instant.tzinfo is not None for p in parsed}
    if len(aware_flags) > 1:
        return [], "Values mix timezone-naive and timezone-aware temporal values — not safely comparable."

    if True in aware_flags:
        offsets = {p.instant.utcoffset() for p in parsed}
        if len(offsets) > 1:
            return [], "Timezone-aware values use inconsistent UTC offsets — not safely comparable without guessing."

    return parsed, None


def _format_instant(instant: datetime, is_date_only: bool, tzinfo) -> str:
    if is_date_only:
        return instant.date().isoformat()
    if tzinfo is not None:
        return instant.replace(tzinfo=tzinfo).isoformat()
    return instant.isoformat()


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_calendar_months(d: date, months: int, *, end_of_month: bool) -> date | None:
    total = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last_day = _last_day_of_month(year, month)
    if end_of_month:
        day = last_day
    else:
        day = min(d.day, last_day)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _add_calendar_years(d: date, years: int) -> date | None:
    try:
        return date(d.year + years, d.month, d.day)
    except ValueError:
        return None  # e.g. Feb 29 landing on a non-leap target year


def discover_temporal_evidence(
    *,
    target_column: str,
    observed_values: Sequence[Any],
    min_observations: int = 5,
    min_supporting_transitions: int = _MIN_SUPPORTING_TRANSITIONS,
) -> TemporalEvidenceResult:
    """The one entry point. `observed_values` is the full, possibly-
    duplicated multiset of one column's real (non-null) values across
    whatever bounded pool a caller has already gathered — analogous to
    evidence_sequence.py's `observed_values`.

    Never raises for malformed/mixed input — unparseable or ambiguous
    (locale-dependent) string values simply fail to parse and are
    excluded, exactly like null/empty values.
    """
    parsed, rejection_reason = _normalize_all(observed_values)
    observed_values_count = len(parsed)

    if rejection_reason is not None:
        return TemporalEvidenceResult(
            status="AMBIGUOUS", target_column=target_column, observed_values_count=observed_values_count,
            best=None, rejected_alternatives=(), reason=rejection_reason,
        )

    if observed_values_count < min_observations:
        return TemporalEvidenceResult(
            status="INSUFFICIENT_GROUP", target_column=target_column, observed_values_count=observed_values_count,
            best=None, rejected_alternatives=(),
            reason=f"Only {observed_values_count} usable temporal observation(s); minimum required is {min_observations}.",
        )

    is_date_only = all(p.is_date_only for p in parsed)
    tzinfo = next((p.instant.tzinfo for p in parsed if p.instant.tzinfo is not None), None)
    instants = [p.instant for p in parsed]
    unique_sorted = sorted(set(instants))

    if len(unique_sorted) < 2:
        return TemporalEvidenceResult(
            status="NO_RELATIONSHIP", target_column=target_column, observed_values_count=observed_values_count,
            best=None, rejected_alternatives=(),
            reason="Only one distinct temporal value observed (a constant), not a progression.",
        )

    multiplicities = Counter(instants)
    duplicate_count = sum(c - 1 for c in multiplicities.values() if c > 1)

    def _confidence(observation_count: int) -> float:
        return min(1.0, observation_count / (min_supporting_transitions * 2))

    fixed = _try_fixed_interval(
        unique_sorted=unique_sorted, duplicate_count=duplicate_count,
        min_supporting_transitions=min_supporting_transitions, is_date_only=is_date_only, tzinfo=tzinfo,
        observed_values_count=observed_values_count, confidence_fn=_confidence,
    )
    if fixed.status == "CANDIDATE":
        return TemporalEvidenceResult(
            status="CANDIDATE", target_column=target_column, observed_values_count=observed_values_count,
            best=fixed.best, rejected_alternatives=(), reason=fixed.reason,
        )

    # CALENDAR_MONTH_INTERVAL / CALENDAR_YEAR_INTERVAL only make sense on
    # whole dates — a nonzero, varying time-of-day component has no
    # calendar-month/year analogue worth guessing at.
    times_of_day = {p.instant.time() for p in parsed}
    if len(times_of_day) == 1:
        dates_unique = sorted({p.instant.date() for p in parsed})
        date_multiplicities = Counter(p.instant.date() for p in parsed)
        date_duplicate_count = sum(c - 1 for c in date_multiplicities.values() if c > 1)

        # CALENDAR_YEAR_INTERVAL is tried first: it requires the strictly
        # stronger condition that BOTH month and day are held constant
        # (not just day-of-month), so whenever it legitimately applies —
        # e.g. an exact 12-calendar-month step recurring on the same
        # month-and-day — it is the more specific and more informative
        # label, and it can never spuriously match data that CALENDAR_
        # MONTH_INTERVAL alone would have correctly handled (a varying
        # month always fails the year convention's same-month-day check
        # immediately, falling straight through with no interference).
        year_result = _try_calendar_year_interval(
            dates_unique=dates_unique, duplicate_count=date_duplicate_count,
            min_supporting_transitions=min_supporting_transitions, is_date_only=is_date_only, tzinfo=tzinfo,
            time_of_day=next(iter(times_of_day)), observed_values_count=observed_values_count,
            confidence_fn=_confidence,
        )
        if year_result.status == "CANDIDATE":
            return TemporalEvidenceResult(
                status="CANDIDATE", target_column=target_column, observed_values_count=observed_values_count,
                best=year_result.best, rejected_alternatives=(), reason=year_result.reason,
            )

        month_result = _try_calendar_month_interval(
            dates_unique=dates_unique, duplicate_count=date_duplicate_count,
            min_supporting_transitions=min_supporting_transitions, is_date_only=is_date_only, tzinfo=tzinfo,
            time_of_day=next(iter(times_of_day)), observed_values_count=observed_values_count,
            confidence_fn=_confidence,
        )
        if month_result.status == "CANDIDATE":
            return TemporalEvidenceResult(
                status="CANDIDATE", target_column=target_column, observed_values_count=observed_values_count,
                best=month_result.best, rejected_alternatives=(), reason=month_result.reason,
            )

    # None of the strategies produced a confident candidate — the
    # FIXED_INTERVAL attempt is the most generically informative failure
    # reason to surface (it is always attempted, unlike the calendar
    # strategies which require uniform time-of-day).
    return TemporalEvidenceResult(
        status=fixed.status, target_column=target_column, observed_values_count=observed_values_count,
        best=None, rejected_alternatives=(), reason=fixed.reason,
    )


@dataclass(frozen=True)
class _StrategyOutcome:
    status: str
    best: TemporalEvidence | None
    reason: str


def _try_fixed_interval(
    *, unique_sorted: list[datetime], duplicate_count: int, min_supporting_transitions: int,
    is_date_only: bool, tzinfo, observed_values_count: int, confidence_fn,
) -> _StrategyOutcome:
    diffs = [unique_sorted[i + 1] - unique_sorted[i] for i in range(len(unique_sorted) - 1)]
    step, mode_count = Counter(diffs).most_common(1)[0]
    step_consistency = mode_count / len(diffs)
    supporting_count = mode_count
    contradicting_count = len(diffs) - mode_count

    if step_consistency < _MIN_STEP_CONSISTENCY_FOR_ANY_PATTERN:
        return _StrategyOutcome(
            "NO_RELATIONSHIP", None,
            f"No consistent time interval: only {step_consistency:.0%} of consecutive value pairs share the "
            f"same step ({step}) — this does not behave like a regular progression.",
        )

    min_value, max_value = unique_sorted[0], unique_sorted[-1]
    if step.total_seconds() <= 0:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Degenerate zero/negative step.")

    expected_slot_count = (max_value - min_value) / step + 1
    if expected_slot_count > _MAX_EXPECTED_RANGE_SLOTS:
        return _StrategyOutcome(
            "NO_RELATIONSHIP", None,
            f"The implied range from {min_value.isoformat()} to {max_value.isoformat()} at step {step} would "
            f"span {expected_slot_count:.0f} slots — almost certainly an outlier rather than a real gap/sequence.",
        )

    expected_slots = set()
    cur = min_value
    while cur <= max_value:
        expected_slots.add(cur)
        cur += step
    gaps = sorted(expected_slots - set(unique_sorted))

    if len(gaps) == 0 and duplicate_count == 0:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Sequence is already fully contiguous — no gap or duplicate to correct.")
    if len(gaps) > 1:
        return _StrategyOutcome(
            "AMBIGUOUS", None,
            f"{len(gaps)} gap positions found — cannot determine which single value applies without more context.",
        )
    if duplicate_count > 1:
        return _StrategyOutcome(
            "AMBIGUOUS", None,
            f"{duplicate_count} excess occurrence(s) among duplicated value(s) — too many simultaneous "
            "duplicates to confidently attribute a single replacement.",
        )
    if supporting_count < min_supporting_transitions:
        return _StrategyOutcome(
            "AMBIGUOUS", None,
            f"Only {supporting_count} consecutive value pair(s) confirm step {step} (minimum "
            f"{min_supporting_transitions} required) — not enough to rule out a step change or an incomplete "
            "dataset rather than a single defensible gap/duplicate.",
        )

    if len(gaps) == 1:
        candidate_instant = gaps[0]
        strategy = "TEMPORAL_GAP"
        description = f"The sequence from {min_value.isoformat()} to {max_value.isoformat()} (step {step}) is missing exactly one value."
    else:
        candidate_instant = max_value + step
        strategy = "TEMPORAL_NEXT_VALUE"
        description = (
            f"Exactly one duplicate value; the sequence from {min_value.isoformat()} to {max_value.isoformat()} "
            f"(step {step}) is otherwise fully contiguous, so extending it by one step is defensible."
        )

    already_exists = candidate_instant in unique_sorted
    candidate_value = _format_instant(candidate_instant, is_date_only, tzinfo)

    if already_exists:
        return _StrategyOutcome("AMBIGUOUS", None, f"Computed candidate {candidate_value!r} already exists among observed values.")

    best = TemporalEvidence(
        strategy=strategy, description=description, candidate_value=candidate_value,
        progression_type="FIXED_INTERVAL", interval=str(step), observed_values_count=observed_values_count,
        duplicate_count=duplicate_count, gap_count=len(gaps), supporting_count=supporting_count,
        contradicting_count=contradicting_count, consistency=step_consistency,
        confidence=confidence_fn(len(unique_sorted)), candidate_already_exists=False,
    )
    return _StrategyOutcome(
        "CANDIDATE", best,
        f"{strategy} confirmed by {supporting_count} consistent transition(s) (step {step}) across "
        f"{len(unique_sorted)} distinct observed values.",
    )


def _try_calendar_month_interval(
    *, dates_unique: list[date], duplicate_count: int, min_supporting_transitions: int,
    is_date_only: bool, tzinfo, time_of_day: time, observed_values_count: int, confidence_fn,
) -> _StrategyOutcome:
    if len(dates_unique) < 2:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Not enough distinct dates for a calendar-month progression.")

    same_day = all(d.day == dates_unique[0].day for d in dates_unique)
    end_of_month = all(d.day == _last_day_of_month(d.year, d.month) for d in dates_unique)

    if not same_day and not end_of_month:
        return _StrategyOutcome(
            "NO_RELATIONSHIP", None,
            "Observed dates do not consistently share a day-of-month or all fall on their month's last day — "
            "no calendar-month convention established.",
        )
    convention_supporting = len(dates_unique)  # every one of them independently confirms the convention

    month_index = lambda d: d.year * 12 + (d.month - 1)  # noqa: E731
    diffs = [month_index(dates_unique[i + 1]) - month_index(dates_unique[i]) for i in range(len(dates_unique) - 1)]
    step, mode_count = Counter(diffs).most_common(1)[0]
    if step <= 0:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Degenerate zero/negative month step.")
    if step > _MAX_EXPECTED_MONTHS:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Implied month step is implausibly large — likely an outlier.")

    total_months_span = month_index(dates_unique[-1]) - month_index(dates_unique[0])
    if total_months_span // step > _MAX_EXPECTED_MONTHS:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Implied month range is implausibly large — likely an outlier.")

    expected_month_indexes = set(range(month_index(dates_unique[0]), month_index(dates_unique[-1]) + step, step))
    present_month_indexes = {month_index(d) for d in dates_unique}
    gap_month_indexes = sorted(expected_month_indexes - present_month_indexes)

    if convention_supporting < min_supporting_transitions:
        return _StrategyOutcome(
            "AMBIGUOUS", None,
            f"Only {convention_supporting} date(s) confirm the calendar-month convention (minimum "
            f"{min_supporting_transitions} required).",
        )
    if len(gap_month_indexes) == 0 and duplicate_count == 0:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Calendar-month sequence is already fully contiguous.")
    if len(gap_month_indexes) > 1:
        return _StrategyOutcome("AMBIGUOUS", None, f"{len(gap_month_indexes)} calendar-month gaps found — cannot determine which one applies.")
    if duplicate_count > 1:
        return _StrategyOutcome("AMBIGUOUS", None, f"{duplicate_count} excess duplicate date(s) — too many to confidently resolve.")

    if len(gap_month_indexes) == 1:
        gap_year, gap_month0 = divmod(gap_month_indexes[0], 12)
        gap_month = gap_month0 + 1
        last_day = _last_day_of_month(gap_year, gap_month)
        day = last_day if end_of_month else min(dates_unique[0].day, last_day)
        try:
            candidate_date = date(gap_year, gap_month, day)
        except ValueError:
            candidate_date = None
        strategy = "TEMPORAL_GAP"
        description = "Calendar-month progression is missing exactly one month."
    else:
        candidate_date = _add_calendar_months(dates_unique[-1], step, end_of_month=end_of_month)
        strategy = "TEMPORAL_NEXT_VALUE"
        description = "Exactly one duplicate date in an otherwise contiguous calendar-month progression."

    if candidate_date is None:
        return _StrategyOutcome("AMBIGUOUS", None, "The implied calendar-month candidate is not a valid calendar date.")

    candidate_instant = datetime.combine(candidate_date, time_of_day)
    already_exists = candidate_date in dates_unique
    candidate_value = _format_instant(candidate_instant, is_date_only, tzinfo)

    if already_exists:
        return _StrategyOutcome("AMBIGUOUS", None, f"Computed candidate {candidate_value!r} already exists among observed values.")

    convention_label = "end-of-month" if end_of_month else "same day-of-month"
    best = TemporalEvidence(
        strategy=strategy, description=description, candidate_value=candidate_value,
        progression_type="CALENDAR_MONTH_INTERVAL", interval=f"{step} calendar month(s) ({convention_label})",
        observed_values_count=observed_values_count, duplicate_count=duplicate_count, gap_count=len(gap_month_indexes),
        supporting_count=convention_supporting, contradicting_count=0, consistency=1.0,
        confidence=confidence_fn(len(dates_unique)), candidate_already_exists=False,
    )
    return _StrategyOutcome(
        "CANDIDATE", best,
        f"{strategy} confirmed by {convention_supporting} date(s) sharing a {convention_label} convention.",
    )


def _try_calendar_year_interval(
    *, dates_unique: list[date], duplicate_count: int, min_supporting_transitions: int,
    is_date_only: bool, tzinfo, time_of_day: time, observed_values_count: int, confidence_fn,
) -> _StrategyOutcome:
    if len(dates_unique) < 2:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Not enough distinct dates for a calendar-year progression.")

    same_month_day = all((d.month, d.day) == (dates_unique[0].month, dates_unique[0].day) for d in dates_unique)
    if not same_month_day:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Observed dates do not share a common month-and-day — no calendar-year convention established.")
    convention_supporting = len(dates_unique)

    diffs = [dates_unique[i + 1].year - dates_unique[i].year for i in range(len(dates_unique) - 1)]
    step, mode_count = Counter(diffs).most_common(1)[0]
    if step <= 0:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Degenerate zero/negative year step.")
    if step > _MAX_EXPECTED_YEARS:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Implied year step is implausibly large — likely an outlier.")

    span_years = dates_unique[-1].year - dates_unique[0].year
    if span_years // step > _MAX_EXPECTED_YEARS:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Implied year range is implausibly large — likely an outlier.")

    expected_years = set(range(dates_unique[0].year, dates_unique[-1].year + step, step))
    present_years = {d.year for d in dates_unique}
    gap_years = sorted(expected_years - present_years)

    if convention_supporting < min_supporting_transitions:
        return _StrategyOutcome(
            "AMBIGUOUS", None,
            f"Only {convention_supporting} date(s) confirm the calendar-year convention (minimum "
            f"{min_supporting_transitions} required).",
        )
    if len(gap_years) == 0 and duplicate_count == 0:
        return _StrategyOutcome("NO_RELATIONSHIP", None, "Calendar-year sequence is already fully contiguous.")
    if len(gap_years) > 1:
        return _StrategyOutcome("AMBIGUOUS", None, f"{len(gap_years)} calendar-year gaps found — cannot determine which one applies.")
    if duplicate_count > 1:
        return _StrategyOutcome("AMBIGUOUS", None, f"{duplicate_count} excess duplicate date(s) — too many to confidently resolve.")

    if len(gap_years) == 1:
        candidate_date = _add_calendar_years(date(gap_years[0], dates_unique[0].month, dates_unique[0].day), 0)
        strategy = "TEMPORAL_GAP"
        description = "Calendar-year progression is missing exactly one year."
    else:
        candidate_date = _add_calendar_years(dates_unique[-1], step)
        strategy = "TEMPORAL_NEXT_VALUE"
        description = "Exactly one duplicate date in an otherwise contiguous calendar-year progression."

    if candidate_date is None:
        return _StrategyOutcome(
            "AMBIGUOUS", None,
            "The implied calendar-year candidate is not a valid calendar date (e.g. Feb 29 on a non-leap year).",
        )

    candidate_instant = datetime.combine(candidate_date, time_of_day)
    already_exists = candidate_date in dates_unique
    candidate_value = _format_instant(candidate_instant, is_date_only, tzinfo)

    if already_exists:
        return _StrategyOutcome("AMBIGUOUS", None, f"Computed candidate {candidate_value!r} already exists among observed values.")

    best = TemporalEvidence(
        strategy=strategy, description=description, candidate_value=candidate_value,
        progression_type="CALENDAR_YEAR_INTERVAL", interval=f"{step} calendar year(s)",
        observed_values_count=observed_values_count, duplicate_count=duplicate_count, gap_count=len(gap_years),
        supporting_count=convention_supporting, contradicting_count=0, consistency=1.0,
        confidence=confidence_fn(len(dates_unique)), candidate_already_exists=False,
    )
    return _StrategyOutcome(
        "CANDIDATE", best,
        f"{strategy} confirmed by {convention_supporting} date(s) sharing month-and-day {dates_unique[0].month:02d}-{dates_unique[0].day:02d}.",
    )

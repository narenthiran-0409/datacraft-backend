"""Sequence / gap / next-value Evidence Engine (Phase 4.2) — pure,
provider-agnostic, column-name-agnostic.

Mirrors app.modules.ai.evidence's isolation rule exactly: no imports from
app.db.models, app.source_adapters, sqlalchemy, Celery, or any AI
orchestration module. Operates only on a plain sequence of already-observed
values passed in by a caller — a later, separate integration phase is
responsible for gathering those values from a live source and deciding
which specific row's issue this evidence applies to. NOT WIRED into
suggestion_service.py, candidates.py, or anything else yet (Phase 4.2 is a
pure, standalone foundation, exactly like Phase 0's evidence.py was).

Why this module's shape deliberately differs from
app.modules.ai.evidence.discover_relationship_evidence: that function needs
a specific failing row's OTHER column values to compute a numeric
candidate (e.g. Qty to derive OrderAmount). Sequence evidence needs no such
per-row context — a gap or a duplicate is a property of the observed value
SET as a whole, not of any one row's other fields. So this module takes
only `observed_values` (the full, possibly-duplicated multiset of one
identifier-like column's real values) rather than a failing_row/
candidate_rows pair. Everything else — the four-status result shape
(CANDIDATE/NO_RELATIONSHIP/AMBIGUOUS/INSUFFICIENT_GROUP), deterministic
pure computation, "never fabricate merely because arithmetic is easy" — is
the same discipline Phase 0 established.

Column-name-agnostic by construction: nothing in this module ever reads or
switches on `target_column` — it is carried through purely as a label on
the result for the caller's convenience (audit trails, logging), exactly
like target_column in RelationshipEvidence/EvidenceResult.

ALGORITHM SUMMARY
------------------
1. Parse every non-null value as optional-prefix + signed-digits +
   optional-suffix (e.g. "ORD007" -> prefix "ORD", value 7; "-500" ->
   value -500). Values that don't match this shape at all (no digits)
   simply don't join any template.
2. Group by (prefix, suffix). The largest group must cover at least
   _MIN_TEMPLATE_COVERAGE of ALL non-null observations (not just the
   parseable ones) to be treated as a genuine, consistent identifier
   scheme — a column full of unrelated formats, or one dominated by
   unparseable noise, is NO_RELATIONSHIP, not a weak sequence.
3. Within that group, find the most common consecutive-value step. Below
   _MIN_STEP_CONSISTENCY_FOR_ANY_PATTERN of consecutive pairs agreeing on
   that step, there is no discernible pattern at all -> NO_RELATIONSHIP
   (this is what correctly rejects genuinely non-sequential identifiers
   such as A100/A900/B123/XYZ77, and wildly irregular data such as
   101/205/301/450).
4. Using that step, find gaps (expected-but-absent slots) and duplicates
   (values appearing more than once). Multiple simultaneous gaps, or
   multiple simultaneous duplicates, are always AMBIGUOUS — never an
   arbitrary pick among them. A column that's already perfectly
   contiguous with no duplicate is NO_RELATIONSHIP ("nothing to fix").
5. With EXACTLY one gap (or, absent a gap, exactly one duplicated value),
   a candidate is considered ONLY if enough consecutive transitions
   independently confirm the step (see _MIN_SUPPORTING_TRANSITIONS below)
   — a single gap in a four-value run and a single gap in an
   eight-value run are NOT treated the same, because the former can't
   rule out "the step changed" or "the dataset is simply incomplete"
   nearly as well as the latter can (see the "1, 2, 3, 5" vs.
   "1,2,3,4,6,7,8" cases in the test suite). This is deliberately an
   ABSOLUTE-COUNT gate, not a ratio: a single anomaly in an otherwise
   clean run of any length above the floor is trustworthy; a ratio-based
   gate would unfairly demand ever-longer runs to tolerate the one
   anomaly it is specifically trying to explain.
6. A pathologically large candidate range (e.g. one wild outlier next to
   a tight cluster) is capped rather than ever materialized in full — see
   _MAX_EXPECTED_RANGE_SLOTS.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

# A value must match this pattern IN FULL (both ends anchored) to be
# considered identifier-like at all: an optional non-digit, non-minus
# prefix, then a signed-or-unsigned run of digits, then an optional
# non-digit suffix. "-" is deliberately excluded from the prefix/suffix
# character classes so a genuine negative number ("-500") is parsed as
# prefix="", numeric=-500, suffix="" rather than losing its sign to a
# greedy "-"-eating prefix.
_IDENTIFIER_PATTERN = re.compile(r"^([^0-9-]*)(-?\d+)([^0-9-]*)$")

# A value/template family covering less than this fraction of ALL non-null
# observations (not just the ones that parsed at all) is not "dominant" —
# the column is heterogeneous, not a single consistent identifier scheme.
_MIN_TEMPLATE_COVERAGE = 0.9

# Below this fraction of consecutive unique-value pairs sharing the single
# most common step, there is no discernible pattern at all.
_MIN_STEP_CONSISTENCY_FOR_ANY_PATTERN = 0.5

# Absolute (not ratio) floor on how many consecutive transitions must
# independently confirm the established step before a single gap/duplicate
# is trusted enough to produce a candidate. See the module docstring's
# ALGORITHM SUMMARY step 5 for why this is a count, not a ratio.
_MIN_SUPPORTING_TRANSITIONS = 3

# Safety cap: never materialize an expected-slot range wider than this —
# an isolated wild outlier (e.g. one value of 9999999 next to a tight
# cluster around 1000) must be rejected cheaply, not by building a
# multi-million-entry set.
_MAX_EXPECTED_RANGE_SLOTS = 10_000


@dataclass(frozen=True)
class SequenceEvidence:
    """One defensible candidate found by the sequence/gap engine.

    Field meanings mirror app.modules.ai.evidence.RelationshipEvidence
    where the concepts overlap (candidate_value, confidence), and add the
    sequence-specific fields the Phase 4.2 spec asks for."""

    strategy: str  # "SEQUENCE_GAP" | "SEQUENCE_NEXT_VALUE"
    description: str
    candidate_value: str  # always a formatted string, template-aware (e.g. "ORD004")
    observed_pattern: str
    template: str | None  # e.g. "ORD###", or None for plain numeric
    step: int
    min_value: int
    max_value: int
    gap_count: int
    duplicate_count: int  # total excess occurrences (count - 1, summed) across all repeated values
    observed_values_count: int  # total non-null raw observations considered (includes duplicates)
    supporting_count: int  # consecutive unique-value pairs matching the established step
    contradicting_count: int  # consecutive unique-value pairs NOT matching it
    step_consistency: float  # supporting_count / (supporting_count + contradicting_count)
    coverage_ratio: float  # expected slots actually observed / total expected slots
    confidence: float
    candidate_already_exists: bool  # always False when this is `best` — see SequenceEvidenceResult


@dataclass(frozen=True)
class SequenceEvidenceResult:
    status: str  # "CANDIDATE" | "NO_RELATIONSHIP" | "AMBIGUOUS" | "INSUFFICIENT_GROUP"
    target_column: str
    observed_values_count: int
    best: SequenceEvidence | None
    rejected_alternatives: tuple[SequenceEvidence, ...]
    reason: str


@dataclass(frozen=True)
class _ParsedIdentifier:
    raw: str
    prefix: str
    suffix: str
    numeric_value: int
    digit_width: int  # length of the digit run as originally written (captures zero-padding)


def _parse_identifier(value: Any) -> _ParsedIdentifier | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _IDENTIFIER_PATTERN.match(text)
    if match is None:
        return None
    prefix, digits, suffix = match.groups()
    try:
        numeric_value = int(digits)
    except ValueError:
        return None
    # digit_width reflects the digit run only (sign not counted) — used
    # purely to decide whether the template is fixed-width zero-padded.
    digit_run = digits.lstrip("-")
    return _ParsedIdentifier(raw=text, prefix=prefix, suffix=suffix, numeric_value=numeric_value, digit_width=len(digit_run))


def _dominant_template(
    parsed: list[_ParsedIdentifier], total_non_null: int
) -> tuple[tuple[str, str], list[_ParsedIdentifier]] | None:
    """Groups parsed identifiers by (prefix, suffix) and returns the
    dominant group and its members, or None if no group reaches
    _MIN_TEMPLATE_COVERAGE of ALL non-null observations — deliberately
    against `total_non_null`, not `len(parsed)`, so a column dominated by
    unparseable noise (values with no digits at all) is correctly treated
    as having low coverage rather than looking artificially "100% of what
    parsed" consistent."""
    if not parsed or total_non_null == 0:
        return None
    groups: dict[tuple[str, str], list[_ParsedIdentifier]] = {}
    for p in parsed:
        groups.setdefault((p.prefix, p.suffix), []).append(p)
    template, members = max(groups.items(), key=lambda kv: len(kv[1]))
    coverage = len(members) / total_non_null
    if coverage < _MIN_TEMPLATE_COVERAGE:
        return None
    return template, members


def _format_candidate(template: tuple[str, str], numeric_value: int, members: list[_ParsedIdentifier]) -> str:
    prefix, suffix = template
    widths = {m.digit_width for m in members}
    digits_str = str(abs(numeric_value))
    if len(widths) == 1:
        fixed_width = next(iter(widths))
        if fixed_width > len(digits_str):
            digits_str = digits_str.zfill(fixed_width)
    sign = "-" if numeric_value < 0 else ""
    return f"{prefix}{sign}{digits_str}{suffix}"


def _render_template_label(template: tuple[str, str], members: list[_ParsedIdentifier]) -> str | None:
    prefix, suffix = template
    if not prefix and not suffix:
        return None
    widths = {m.digit_width for m in members}
    width_marker = "#" * next(iter(widths)) if len(widths) == 1 else "#+"
    return f"{prefix}{width_marker}{suffix}"


def discover_sequence_evidence(
    *,
    target_column: str,
    observed_values: Sequence[Any],
    min_observations: int = 5,
    min_supporting_transitions: int = _MIN_SUPPORTING_TRANSITIONS,
    ambiguous_step_consistency: float = _MIN_STEP_CONSISTENCY_FOR_ANY_PATTERN,
) -> SequenceEvidenceResult:
    """The one entry point. `observed_values` is the full, possibly-
    duplicated multiset of one column's real (non-null) values across
    whatever bounded pool a caller has already gathered — analogous to
    app.modules.ai.evidence's `candidate_rows`, already narrowed to this
    one column's values.

    Never raises for malformed/mixed input — unparseable values simply
    fail to join any template and count against template coverage.
    """
    non_null = [v for v in observed_values if v is not None and str(v).strip() != ""]
    observed_values_count = len(non_null)

    if observed_values_count < min_observations:
        return SequenceEvidenceResult(
            status="INSUFFICIENT_GROUP",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=f"Only {observed_values_count} non-null observation(s); minimum required is {min_observations}.",
        )

    parsed = [p for p in (_parse_identifier(v) for v in non_null) if p is not None]

    dominant = _dominant_template(parsed, observed_values_count)
    if dominant is None:
        return SequenceEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                "No single identifier template (prefix/suffix scheme) covers enough of the observed "
                f"values ({len(parsed)}/{observed_values_count} parsed at all) to establish a consistent scheme."
            ),
        )
    template, members = dominant

    numeric_values = [m.numeric_value for m in members]
    unique_sorted = sorted(set(numeric_values))

    if len(unique_sorted) < 2:
        return SequenceEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason="Only one distinct value observed (a constant), not a sequence.",
        )

    multiplicities = Counter(numeric_values)
    duplicate_count = sum(count - 1 for count in multiplicities.values() if count > 1)
    duplicated_values = sorted(v for v, c in multiplicities.items() if c > 1)

    diffs = [unique_sorted[i + 1] - unique_sorted[i] for i in range(len(unique_sorted) - 1)]
    step, mode_count = Counter(diffs).most_common(1)[0]
    step_consistency = mode_count / len(diffs)
    supporting_count = mode_count
    contradicting_count = len(diffs) - mode_count

    if step_consistency < ambiguous_step_consistency:
        return SequenceEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"No consistent step: only {step_consistency:.0%} of consecutive value pairs share the "
                f"same step ({step}) — this does not behave like a sequence."
            ),
        )

    min_value, max_value = unique_sorted[0], unique_sorted[-1]

    # step > 0 is guaranteed here: unique_sorted is strictly increasing
    # (sorted(set(...))), so every diff is > 0 and so is its mode.
    expected_slot_count = (max_value - min_value) // step + 1
    if expected_slot_count > _MAX_EXPECTED_RANGE_SLOTS:
        return SequenceEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"The implied range from {min_value} to {max_value} at step {step} would span "
                f"{expected_slot_count} slots — almost certainly an outlier rather than a real gap/sequence."
            ),
        )

    expected_slots = set(range(min_value, max_value + step, step))
    gaps = sorted(expected_slots - set(unique_sorted))
    present_expected = expected_slots & set(unique_sorted)
    coverage_ratio = len(present_expected) / len(expected_slots) if expected_slots else 1.0

    template_label = _render_template_label(template, members)
    observed_pattern = (
        f"{'template ' + template_label + ' ' if template_label else ''}"
        f"numeric step={step}, {len(unique_sorted)} distinct value(s) from {min_value} to {max_value}"
    )

    def _confidence(base: float, observation_count: int) -> float:
        # Same size-discount spirit as evidence.py's _confidence(): more
        # observations -> closer to the raw step_consistency; a bare
        # minimum sample gets discounted toward half of it.
        size_factor = min(1.0, observation_count / (min_observations * 2))
        return base * size_factor

    def _build(strategy: str, candidate_numeric: int, description: str) -> SequenceEvidence:
        return SequenceEvidence(
            strategy=strategy,
            description=description,
            candidate_value=_format_candidate(template, candidate_numeric, members),
            observed_pattern=observed_pattern,
            template=template_label,
            step=step,
            min_value=min_value,
            max_value=max_value,
            gap_count=len(gaps),
            duplicate_count=duplicate_count,
            observed_values_count=observed_values_count,
            supporting_count=supporting_count,
            contradicting_count=contradicting_count,
            step_consistency=step_consistency,
            coverage_ratio=coverage_ratio,
            confidence=_confidence(step_consistency, len(unique_sorted)),
            candidate_already_exists=candidate_numeric in unique_sorted,
        )

    if len(gaps) == 0 and duplicate_count == 0:
        return SequenceEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason="Sequence is already fully contiguous — no gap or duplicate to correct.",
        )

    if len(gaps) > 1:
        return SequenceEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"{len(gaps)} gap positions found ({gaps}) — cannot determine which single value applies "
                "without more context."
            ),
        )

    if duplicate_count > 1:
        return SequenceEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"{duplicate_count} excess occurrence(s) across duplicated value(s) {duplicated_values} — "
                "too many simultaneous duplicates to confidently attribute a single replacement."
            ),
        )

    # Exactly one gap XOR exactly one duplicate from here.
    if supporting_count < min_supporting_transitions:
        return SequenceEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"Only {supporting_count} consecutive value pair(s) confirm step {step} (minimum "
                f"{min_supporting_transitions} required) — not enough to rule out a step change or an "
                "incomplete dataset rather than a single defensible gap/duplicate."
            ),
        )

    if len(gaps) == 1:
        candidate_numeric = gaps[0]
        candidate = _build(
            "SEQUENCE_GAP", candidate_numeric,
            f"The sequence from {min_value} to {max_value} (step {step}) is missing exactly one value.",
        )
    else:
        candidate_numeric = max_value + step
        candidate = _build(
            "SEQUENCE_NEXT_VALUE", candidate_numeric,
            (
                f"Value {duplicated_values[0]} is duplicated once; the sequence from {min_value} to "
                f"{max_value} (step {step}) is otherwise fully contiguous, so extending it by one step "
                "is the defensible replacement for the duplicate."
            ),
        )

    if candidate.candidate_already_exists:
        # Defensive guard: by construction (gap = absent slot; next-value =
        # beyond the observed max) this should never actually trigger, but
        # a computed candidate must never be proposed if it turns out to
        # collide with an already-observed value.
        return SequenceEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            observed_values_count=observed_values_count,
            best=None,
            rejected_alternatives=(candidate,),
            reason=f"Computed candidate {candidate.candidate_value!r} already exists among observed values.",
        )

    return SequenceEvidenceResult(
        status="CANDIDATE",
        target_column=target_column,
        observed_values_count=observed_values_count,
        best=candidate,
        rejected_alternatives=(),
        reason=(
            f"{candidate.strategy} confirmed by {candidate.supporting_count} consistent transition(s) "
            f"(step {step}) across {len(unique_sorted)} distinct observed values."
        ),
    )

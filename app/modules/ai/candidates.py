"""Candidate aggregation (Phase 4.1) — pure, provider-agnostic foundation.

Deliberately independent of everything else in this application, mirroring
app.modules.ai.evidence's own isolation rule: no imports from app.db.models,
app.source_adapters, sqlalchemy, Celery, or any AI orchestration module.
Operates only on plain, already-computed values passed in by a caller.

THIS MODULE IS NOT YET WIRED INTO ANYTHING. Nothing in suggestion_service.py,
evidence.py, or any API route constructs a Candidate or calls
aggregate_candidates() yet — that wiring is explicit, later Phase 4 work
(see the Phase 4A investigation report), gated behind
settings.AI_CORRECTION_ADVANCED_INFERENCE_ENABLED (default False). This
phase only establishes the shape multiple future evidence strategies
(sequence/gap, string/template, date-progression, and the existing Phase 0
numeric relationships) will eventually produce and be ranked through.

Design intent (for the sub-phases that will build on this):
  - Each evidence strategy (app.modules.ai.evidence's 5 numeric shapes,
    and later sequence/template/date modules) produces zero or more
    Candidate objects, one per distinct value it's willing to propose.
  - aggregate_candidates() ranks them deterministically by confidence
    (ties broken by strategy name, then value, for full reproducibility —
    never by insertion order alone, which real callers cannot rely on).
  - The LLM (a later sub-phase) may only ever adopt or decline the
    recommended_candidate — exactly the same hallucination-prevention
    posture Phase 0-3 already established for the single-candidate case.
  - When two or more top-ranked candidates propose materially different
    values, aggregation must not silently pick one — see
    AggregationResult.ambiguous / contradicting_candidates below.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class Candidate:
    """One proposed replacement value from one evidence strategy.

    value: the proposed replacement, already formatted as a string (the
    same convention correction_suggestions.suggested_value already uses)
    — never a raw Python number/Decimal, so every strategy's output is
    directly comparable and directly persistable without a caller having
    to know which strategy produced it.

    strategy: a short, stable, uppercase identifier for the strategy that
    produced this candidate (e.g. "RATIO_CONSISTENCY",
    "CONSTANT_WITHIN_GROUP", "SEQUENCE_GAP", "STRING_TEMPLATE",
    "DATE_PROGRESSION" — the first two mirror app.modules.ai.evidence's
    existing RelationshipEvidence.relationship_type values; later
    sub-phases add the rest). Never a free-text description.

    confidence: 0-1, this strategy's own honest estimate — same scale and
    same "more conservative than raw fit quality for small groups" spirit
    as app.modules.ai.evidence.RelationshipEvidence.confidence.

    supporting_count / contradicting_count: how many observed rows agree
    with vs. contradict this candidate's underlying pattern — a strategy
    that found some agreement and some disagreement should report both,
    not silently drop the contradicting evidence.

    evidence: a small, JSON-serializable, aggregate-only dict describing
    *why* — never raw source rows or other columns' full values, the same
    privacy boundary app.modules.ai.context.build_correction_context
    already enforces. Each strategy defines its own evidence shape; this
    module does not prescribe one beyond "must be JSON-serializable."
    """

    value: str
    strategy: str
    confidence: float
    supporting_count: int = 0
    contradicting_count: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("Candidate.value must be a non-empty string")
        if not self.strategy:
            raise ValueError("Candidate.strategy must be a non-empty string")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"Candidate.confidence must be within [0, 1], got {self.confidence!r}")
        if self.supporting_count < 0 or self.contradicting_count < 0:
            raise ValueError("Candidate supporting_count/contradicting_count must be >= 0")


@dataclass(frozen=True)
class AggregationResult:
    """The outcome of ranking zero or more Candidates for a single issue.

    candidates: every candidate that was produced, sorted by confidence
    descending (ties broken by strategy name, then value — deterministic,
    never dependent on input order or dict iteration order).

    recommended_candidate: the single best candidate, or None if either no
    strategy produced one, or the top candidates disagree materially (see
    `ambiguous`). This is the ONLY candidate a later AI-reasoning step may
    adopt — never one it invents itself and never any other entry in
    `candidates`.

    ambiguous: True when two or more candidates are tied (or within an
    ambiguity margin — the exact margin is each future strategy/aggregator
    caller's own concern, not fixed here) for the top rank AND propose
    materially different values. Mirrors
    app.modules.ai.evidence.EvidenceResult's own AMBIGUOUS status for the
    single-strategy case.

    strategies_attempted: every strategy name a caller reports having run,
    REGARDLESS of whether it produced a candidate — needed to answer "did
    the system even check for a sequence/template/date pattern here?"
    (Phase 4K's explicit no-candidate-state requirement) without
    conflating "not attempted" with "attempted and found nothing."

    strategies_with_candidates: subset of strategies_attempted that
    actually produced at least one Candidate.

    no_candidate_reason: a short, human-readable explanation for why
    recommended_candidate is None — only meaningful when it IS None.
    """

    candidates: tuple[Candidate, ...]
    recommended_candidate: Candidate | None
    ambiguous: bool
    strategies_attempted: tuple[str, ...]
    strategies_with_candidates: tuple[str, ...]
    no_candidate_reason: str | None


def aggregate_candidates(
    candidates: Sequence[Candidate],
    *,
    strategies_attempted: Sequence[str],
    ambiguity_margin: float = 0.03,
) -> AggregationResult:
    """Pure, deterministic ranking. Does not call any strategy itself —
    callers (later Phase 4 sub-phases) run each evidence strategy
    themselves and pass in whatever Candidates resulted.

    Ranking: by confidence descending, ties broken by strategy name then
    value (both ascending) — so the same input always produces the same
    order and the same recommended_candidate, run after run.

    Ambiguity: the top candidate is compared against every other candidate
    within `ambiguity_margin` confidence of it. If any of those propose a
    materially different value (not merely a different string
    representation of the same number — e.g. "735" vs "735.0" are treated
    as equal here via a float-aware comparison, falling back to exact
    string equality for non-numeric values), the result is ambiguous and
    recommended_candidate is None — mirroring
    app.modules.ai.evidence.discover_relationship_evidence's own
    AMBIGUOUS handling for the single-strategy case.
    """
    strategies_with_candidates = tuple(
        dict.fromkeys(c.strategy for c in candidates)  # de-duplicated, first-seen order
    )
    ordered = tuple(sorted(candidates, key=lambda c: (-c.confidence, c.strategy, c.value)))

    if not ordered:
        return AggregationResult(
            candidates=(),
            recommended_candidate=None,
            ambiguous=False,
            strategies_attempted=tuple(strategies_attempted),
            strategies_with_candidates=strategies_with_candidates,
            no_candidate_reason="No strategy produced a candidate value.",
        )

    top = ordered[0]
    competitors = [
        c
        for c in ordered[1:]
        if (top.confidence - c.confidence) <= ambiguity_margin and not _values_equal(c.value, top.value)
    ]

    if competitors:
        return AggregationResult(
            candidates=ordered,
            recommended_candidate=None,
            ambiguous=True,
            strategies_attempted=tuple(strategies_attempted),
            strategies_with_candidates=strategies_with_candidates,
            no_candidate_reason=(
                f"{len(competitors) + 1} candidates from different strategies scored within "
                f"{ambiguity_margin:.2f} of each other but disagree on the value — declining to "
                "choose one automatically."
            ),
        )

    return AggregationResult(
        candidates=ordered,
        recommended_candidate=top,
        ambiguous=False,
        strategies_attempted=tuple(strategies_attempted),
        strategies_with_candidates=strategies_with_candidates,
        no_candidate_reason=None,
    )


def _values_equal(a: str, b: str) -> bool:
    """String equality, with a float-aware fallback so "735" and "735.0"
    (the same underlying value, formatted slightly differently by two
    different strategies) are never mistaken for a disagreement."""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False

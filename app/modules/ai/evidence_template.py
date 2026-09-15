"""Cross-column string/template Evidence Engine (Phase 4.3) — pure,
provider-agnostic, column-name-agnostic.

Mirrors app.modules.ai.evidence / evidence_sequence's isolation rule
exactly: no imports from app.db.models, app.source_adapters, sqlalchemy,
Celery, or any AI orchestration module. Operates only on plain
(source, target) string pairs passed in by a caller — a later, separate
integration phase is responsible for gathering those pairs from a live
source and deciding which specific row's issue this evidence applies to.
NOT WIRED into suggestion_service.py, candidates.py, or anything else yet
(Phase 4.3 is a pure, standalone foundation, exactly like Phases 0 and 4.2).

WHAT THIS DISCOVERS
-------------------
Given several observed (source, target) pairs — e.g. a person's name and
their email address — this module tries to learn a single, STABLE string
transformation: tokenize the source, transform each token's case (as-is /
lower / upper), join the tokens with some delimiter ("", ".", "_", "-",
" "), and wrap the result in a constant prefix/suffix. If one such
transformation reproduces EVERY observed pair's target exactly (never
"mostly"), it is trusted enough to compute a candidate for a NEW source
value whose target is missing, invalid, or otherwise not usable.

Nothing about "names", "emails", "gmail.com", "." as a separator, or any
other business convention is hardcoded anywhere in this module — every
concrete choice (which tokens, which case, which delimiter, which
constant prefix/suffix) is discovered by trying a small, fixed menu of
generic candidates against the ACTUAL observed data and keeping only what
is unanimously confirmed. See _CASE_TRANSFORMS / _DELIMITERS for the full
menu; column names and values are never read to steer the search, only
carried through as labels on the result (related_columns, target_column).

ANTI-OVERFITTING / ANTI-HALLUCINATION RULES
--------------------------------------------
  - A transformation is a serious candidate for only ONE source-derived
    hypothesis space per pair (its own case/delimiter/prefix/suffix
    combinations that happen to reproduce ITS OWN target) — but it is
    then cross-validated against EVERY OTHER observed pair, not just the
    one that first suggested it. A hypothesis confirmed by only one pair
    is exactly as easy to contradict as it is to support; this module
    requires an ABSOLUTE minimum number of independently confirming pairs
    (min_supporting_pairs, default 3 — the same "count, not ratio"
    philosophy app.modules.ai.evidence_sequence uses and for the same
    reason: one lucky match proves nothing).
  - A hypothesis that is contradicted by even ONE other observed pair
    (produces a different string than that pair's real target) is
    rejected outright — never "mostly right". This is what correctly
    rejects a competing-domain scenario (two people's emails end in
    "@gmail.com", a third's ends in "@yahoo.com") rather than silently
    picking the majority domain.
  - Multiple hypotheses that are each strongly confirmed but disagree on
    the candidate value for the query source resolve to AMBIGUOUS, never
    an arbitrary pick.
  - The specific row/value under repair never participates in learning
    the transformation — callers pass it separately as `query_source`,
    exactly mirroring app.modules.ai.evidence's failing-row exclusion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")

_CASE_TRANSFORMS: dict[str, Any] = {
    "as_is": lambda s: s,
    "lower": str.lower,
    "upper": str.upper,
}

_DELIMITERS: tuple[str, ...] = ("", ".", "_", "-", " ")

# Absolute (not ratio) floor on how many independently observed pairs must
# confirm a hypothesis before it is trusted — see module docstring.
_MIN_SUPPORTING_PAIRS = 3


@dataclass(frozen=True)
class TemplateEvidence:
    strategy: str  # always "STRING_TEMPLATE"
    description: str
    related_columns: tuple[str, ...]
    candidate_value: str
    template: str  # human-readable rendering of the learned transform
    delimiter: str
    case_transform: str  # "as_is" | "lower" | "upper"
    prefix: str
    suffix: str
    supporting_count: int
    contradicting_count: int
    consistency: float  # supporting_count / (supporting_count + contradicting_count)
    confidence: float


@dataclass(frozen=True)
class TemplateEvidenceResult:
    status: str  # "CANDIDATE" | "NO_RELATIONSHIP" | "AMBIGUOUS" | "INSUFFICIENT_GROUP"
    target_column: str
    related_columns: tuple[str, ...]
    observed_pairs_count: int
    best: TemplateEvidence | None
    rejected_alternatives: tuple[TemplateEvidence, ...]
    reason: str


@dataclass(frozen=True)
class _Hypothesis:
    case_transform: str
    delimiter: str
    prefix: str
    suffix: str


def _tokenize(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    text = str(value).strip()
    if not text:
        return ()
    return tuple(_TOKEN_PATTERN.findall(text))


def _combined_source_text(source_columns: Sequence[Any]) -> str:
    """Multiple related columns are combined into one string before
    tokenizing — this is the entire mechanism by which multi-column
    inference (e.g. FirstName + LastName -> Email) reuses the exact same
    single-column logic: a 1-column source and an N-column source both
    just become "some tokens", with no special-cased combination logic."""
    parts = [str(v).strip() for v in source_columns if v is not None and str(v).strip() != ""]
    return " ".join(parts)


def _apply_hypothesis(hypothesis: _Hypothesis, source_columns: Sequence[Any]) -> str | None:
    tokens = _tokenize(_combined_source_text(source_columns))
    if not tokens:
        return None
    case_fn = _CASE_TRANSFORMS[hypothesis.case_transform]
    core = hypothesis.delimiter.join(case_fn(t) for t in tokens)
    if not core:
        return None
    return f"{hypothesis.prefix}{core}{hypothesis.suffix}"


def _hypotheses_matching_pair(source_columns: Sequence[Any], target: str) -> set[_Hypothesis]:
    """Every (case, delimiter) combination whose transform of this pair's
    OWN source appears as a contiguous, non-empty substring of this pair's
    OWN target — each becomes one candidate hypothesis (with prefix/suffix
    read off from whatever surrounds the match in this target). Cheap:
    at most len(_CASE_TRANSFORMS) * len(_DELIMITERS) = 15 per pair."""
    tokens = _tokenize(_combined_source_text(source_columns))
    if not tokens:
        return set()

    found: set[_Hypothesis] = set()
    for case_name, case_fn in _CASE_TRANSFORMS.items():
        transformed = [case_fn(t) for t in tokens]
        for delimiter in _DELIMITERS:
            core = delimiter.join(transformed)
            if not core:
                continue
            idx = target.find(core)
            if idx == -1:
                continue
            prefix, suffix = target[:idx], target[idx + len(core):]
            found.add(_Hypothesis(case_transform=case_name, delimiter=delimiter, prefix=prefix, suffix=suffix))
    return found


def discover_template_evidence(
    *,
    target_column: str,
    related_columns: Sequence[str],
    comparable_pairs: Sequence[tuple[Sequence[Any], Any]],
    query_source: Sequence[Any],
    min_observations: int = 3,
    min_supporting_pairs: int = _MIN_SUPPORTING_PAIRS,
) -> TemplateEvidenceResult:
    """The one entry point.

    related_columns / query_source / each pair's source tuple all share
    the same arity: length 1 for a single reference column, length N for
    N reference columns used together (see _combined_source_text). Column
    names in `related_columns` are carried through purely as labels on the
    result — nothing here ever branches on them.

    comparable_pairs is the full, already-gathered pool of OTHER rows'
    (source_columns, target_value) observations — analogous to
    app.modules.ai.evidence's candidate_rows / evidence_sequence's
    observed_values. The row actually being corrected is never part of
    this pool; its source value is passed separately as `query_source`,
    and its (possibly null/invalid/malformed) existing target value is
    never needed by this function at all.

    Never raises for malformed/mixed input — null/empty source or target
    values are simply excluded from the learning pool.
    """
    related_columns = tuple(related_columns)
    valid_pairs = [
        (tuple(src), str(tgt))
        for src, tgt in comparable_pairs
        if tgt is not None and str(tgt).strip() != "" and _tokenize(_combined_source_text(src))
    ]
    observed_pairs_count = len(valid_pairs)

    if observed_pairs_count < min_observations:
        return TemplateEvidenceResult(
            status="INSUFFICIENT_GROUP",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason=f"Only {observed_pairs_count} usable (source, target) pair(s); minimum required is {min_observations}.",
        )

    candidate_hypotheses: set[_Hypothesis] = set()
    for src, tgt in valid_pairs:
        candidate_hypotheses |= _hypotheses_matching_pair(src, tgt)

    if not candidate_hypotheses:
        return TemplateEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason="No case/delimiter transform of the source values appears anywhere in the target values.",
        )

    scored: list[tuple[_Hypothesis, int, int]] = []  # (hypothesis, supporting, contradicting)
    for hypothesis in candidate_hypotheses:
        supporting = 0
        contradicting = 0
        for src, tgt in valid_pairs:
            predicted = _apply_hypothesis(hypothesis, src)
            if predicted == tgt:
                supporting += 1
            else:
                contradicting += 1
        scored.append((hypothesis, supporting, contradicting))

    # Only hypotheses with ZERO contradictions are trustworthy at all —
    # "mostly right" is not a defensible bar for inventing a replacement
    # value (see module docstring). Deterministic ordering: by supporting
    # count desc, then the hypothesis fields themselves, never by
    # incidental set-iteration order.
    perfect = sorted(
        (h for h, s, c in scored if c == 0),
        key=lambda h: (
            -next(s for hh, s, c in scored if hh == h),
            h.case_transform, h.delimiter, h.prefix, h.suffix,
        ),
    )

    if not perfect:
        return TemplateEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"{len(candidate_hypotheses)} candidate transform(s) found, but every one is contradicted "
                "by at least one observed pair — no single transformation explains all the data (e.g. a "
                "competing template/domain)."
            ),
        )

    support_of = {h: s for h, s, c in scored}
    top = perfect[0]
    top_support = support_of[top]

    if top_support < min_supporting_pairs:
        return TemplateEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"The best transformation is confirmed by only {top_support} pair(s) (minimum "
                f"{min_supporting_pairs} required) — not enough independent support to trust it, even "
                "though it is not directly contradicted."
            ),
        )

    query_tokens = _tokenize(_combined_source_text(query_source))
    if not query_tokens:
        return TemplateEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason="The reference source value has no usable tokens to transform.",
        )

    top_candidate_value = _apply_hypothesis(top, query_source)

    # A perfectly-confirmed competitor that disagrees on the query's own
    # candidate value is a real conflict, even though each individually
    # passed the "zero contradictions among training pairs" bar.
    competitors = [
        h for h in perfect[1:]
        if support_of[h] >= min_supporting_pairs and _apply_hypothesis(h, query_source) != top_candidate_value
    ]
    if competitors:
        return TemplateEvidenceResult(
            status="AMBIGUOUS",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason=(
                f"{len(competitors) + 1} equally well-confirmed transformations disagree on the candidate "
                "for this specific source value — declining to choose one automatically."
            ),
        )

    if top_candidate_value is None:
        # Only possible if the query's tokens produced an empty core,
        # already ruled out above — defensive, not reachable in practice.
        return TemplateEvidenceResult(
            status="NO_RELATIONSHIP",
            target_column=target_column,
            related_columns=related_columns,
            observed_pairs_count=observed_pairs_count,
            best=None,
            rejected_alternatives=(),
            reason="The learned transformation could not be applied to this reference source value.",
        )

    def _confidence(observation_count: int) -> float:
        # Same size-discount spirit as evidence.py/evidence_sequence.py.
        return min(1.0, observation_count / (min_supporting_pairs * 2))

    template_label = f"{'lower' if top.case_transform == 'lower' else top.case_transform}(tokens) joined by {top.delimiter!r}"
    if top.prefix or top.suffix:
        template_label += f", wrapped as {top.prefix!r} + core + {top.suffix!r}"

    best = TemplateEvidence(
        strategy="STRING_TEMPLATE",
        description=(
            f"{top_support} observed pair(s) are exactly reproduced by transforming the source's tokens "
            f"({top.case_transform} case, joined with {top.delimiter!r}) and wrapping them as "
            f"{top.prefix!r} + core + {top.suffix!r}, with zero contradicting pairs."
        ),
        related_columns=related_columns,
        candidate_value=top_candidate_value,
        template=template_label,
        delimiter=top.delimiter,
        case_transform=top.case_transform,
        prefix=top.prefix,
        suffix=top.suffix,
        supporting_count=top_support,
        contradicting_count=0,
        consistency=1.0,
        confidence=_confidence(top_support),
    )

    return TemplateEvidenceResult(
        status="CANDIDATE",
        target_column=target_column,
        related_columns=related_columns,
        observed_pairs_count=observed_pairs_count,
        best=best,
        rejected_alternatives=(),
        reason=f"STRING_TEMPLATE confirmed by {top_support} pair(s) with zero contradictions.",
    )

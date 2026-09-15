"""Unit tests for Phase 4.1: the pure, unwired candidate-aggregation
foundation in app.modules.ai.candidates. Nothing here touches the DB,
Celery, or any live pipeline — mirrors tests/unit/test_evidence_engine.py's
own style and isolation for the same reason (this module is exactly that
kind of pure, deterministic building block, one level up)."""
import pytest

from app.modules.ai.candidates import Candidate, aggregate_candidates


# ---------------------------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------------------------


def test_candidate_rejects_empty_value():
    with pytest.raises(ValueError):
        Candidate(value="", strategy="RATIO_CONSISTENCY", confidence=0.9)


def test_candidate_rejects_empty_strategy():
    with pytest.raises(ValueError):
        Candidate(value="735", strategy="", confidence=0.9)


@pytest.mark.parametrize("confidence", [-0.01, 1.01, -5.0, 100.0])
def test_candidate_rejects_out_of_range_confidence(confidence):
    with pytest.raises(ValueError):
        Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=confidence)


def test_candidate_accepts_boundary_confidences():
    Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.0)
    Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=1.0)


def test_candidate_rejects_negative_supporting_count():
    with pytest.raises(ValueError):
        Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.9, supporting_count=-1)


def test_candidate_rejects_negative_contradicting_count():
    with pytest.raises(ValueError):
        Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.9, contradicting_count=-1)


def test_candidate_evidence_defaults_to_empty_dict():
    c = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.9)
    assert c.evidence == {}


# ---------------------------------------------------------------------------
# aggregate_candidates — empty / single
# ---------------------------------------------------------------------------


def test_aggregate_empty_candidates_produces_no_candidate_reason():
    result = aggregate_candidates([], strategies_attempted=["SEQUENCE_GAP", "STRING_TEMPLATE"])
    assert result.candidates == ()
    assert result.recommended_candidate is None
    assert result.ambiguous is False
    assert result.strategies_attempted == ("SEQUENCE_GAP", "STRING_TEMPLATE")
    assert result.strategies_with_candidates == ()
    assert result.no_candidate_reason is not None
    assert "no strategy" in result.no_candidate_reason.lower()


def test_aggregate_single_candidate_is_recommended():
    candidate = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.95)
    result = aggregate_candidates([candidate], strategies_attempted=["RATIO_CONSISTENCY"])
    assert result.recommended_candidate == candidate
    assert result.ambiguous is False
    assert result.no_candidate_reason is None
    assert result.strategies_with_candidates == ("RATIO_CONSISTENCY",)


# ---------------------------------------------------------------------------
# aggregate_candidates — clear winner
# ---------------------------------------------------------------------------


def test_aggregate_picks_highest_confidence_as_recommended():
    low = Candidate(value="1016", strategy="STRING_TEMPLATE", confidence=0.4)
    high = Candidate(value="1015", strategy="SEQUENCE_GAP", confidence=0.97)
    result = aggregate_candidates([low, high], strategies_attempted=["SEQUENCE_GAP", "STRING_TEMPLATE"])
    assert result.recommended_candidate == high
    assert result.ambiguous is False
    # Sorted by confidence descending regardless of input order.
    assert result.candidates == (high, low)


def test_aggregate_strategies_attempted_includes_zero_candidate_strategies():
    high = Candidate(value="1015", strategy="SEQUENCE_GAP", confidence=0.97)
    result = aggregate_candidates(
        [high], strategies_attempted=["SEQUENCE_GAP", "STRING_TEMPLATE", "DATE_PROGRESSION"]
    )
    assert result.strategies_attempted == ("SEQUENCE_GAP", "STRING_TEMPLATE", "DATE_PROGRESSION")
    assert result.strategies_with_candidates == ("SEQUENCE_GAP",)


# ---------------------------------------------------------------------------
# aggregate_candidates — ambiguity
# ---------------------------------------------------------------------------


def test_aggregate_marks_ambiguous_when_close_candidates_disagree():
    a = Candidate(value="1015", strategy="SEQUENCE_GAP", confidence=0.90)
    b = Candidate(value="1099", strategy="STRING_TEMPLATE", confidence=0.89)
    result = aggregate_candidates([a, b], strategies_attempted=["SEQUENCE_GAP", "STRING_TEMPLATE"])
    assert result.ambiguous is True
    assert result.recommended_candidate is None
    assert "disagree" in result.no_candidate_reason.lower()
    # Both candidates are still surfaced, sorted, for the caller to inspect.
    assert result.candidates == (a, b)


def test_aggregate_not_ambiguous_when_gap_exceeds_margin():
    a = Candidate(value="1015", strategy="SEQUENCE_GAP", confidence=0.97)
    b = Candidate(value="1099", strategy="STRING_TEMPLATE", confidence=0.50)
    result = aggregate_candidates([a, b], strategies_attempted=["SEQUENCE_GAP", "STRING_TEMPLATE"])
    assert result.ambiguous is False
    assert result.recommended_candidate == a


def test_aggregate_not_ambiguous_when_close_candidates_agree_on_value():
    # Two strategies independently arriving at the same real value is
    # corroboration, not disagreement — must never be flagged ambiguous.
    a = Candidate(value="735", strategy="CONSTANT_WITHIN_GROUP", confidence=0.91)
    b = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.90)
    result = aggregate_candidates([a, b], strategies_attempted=["CONSTANT_WITHIN_GROUP", "RATIO_CONSISTENCY"])
    assert result.ambiguous is False
    assert result.recommended_candidate is not None
    assert result.recommended_candidate.value == "735"


def test_aggregate_value_equality_is_float_aware():
    # "735" and "735.0" are the same underlying number from two strategies
    # that happened to format it differently — must not be flagged as a
    # disagreement.
    a = Candidate(value="735", strategy="CONSTANT_WITHIN_GROUP", confidence=0.91)
    b = Candidate(value="735.0", strategy="RATIO_CONSISTENCY", confidence=0.90)
    result = aggregate_candidates([a, b], strategies_attempted=["CONSTANT_WITHIN_GROUP", "RATIO_CONSISTENCY"])
    assert result.ambiguous is False


def test_aggregate_non_numeric_values_fall_back_to_string_equality():
    a = Candidate(value="suresh.kumar@gmail.com", strategy="STRING_TEMPLATE", confidence=0.85)
    b = Candidate(value="s.kumar@gmail.com", strategy="STRING_TEMPLATE", confidence=0.84)
    result = aggregate_candidates([a, b], strategies_attempted=["STRING_TEMPLATE"])
    assert result.ambiguous is True  # genuinely different email strings, not numerically comparable


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_aggregate_is_deterministic_across_repeated_calls():
    candidates = [
        Candidate(value="1015", strategy="SEQUENCE_GAP", confidence=0.7),
        Candidate(value="1020", strategy="STRING_TEMPLATE", confidence=0.7),
        Candidate(value="1099", strategy="DATE_PROGRESSION", confidence=0.95),
    ]
    results = [
        aggregate_candidates(candidates, strategies_attempted=["SEQUENCE_GAP", "STRING_TEMPLATE", "DATE_PROGRESSION"])
        for _ in range(5)
    ]
    orderings = {tuple(c.value for c in r.candidates) for r in results}
    assert len(orderings) == 1  # exact same order every time
    recommended = {r.recommended_candidate.value for r in results}
    assert recommended == {"1099"}


def test_aggregate_tie_break_is_deterministic_by_strategy_then_value():
    # Identical confidence, identical (equal) value — order must still be
    # stable, decided by strategy name then value, never input order.
    a = Candidate(value="735", strategy="RATIO_CONSISTENCY", confidence=0.9)
    b = Candidate(value="735", strategy="CONSTANT_WITHIN_GROUP", confidence=0.9)
    result_1 = aggregate_candidates([a, b], strategies_attempted=["RATIO_CONSISTENCY", "CONSTANT_WITHIN_GROUP"])
    result_2 = aggregate_candidates([b, a], strategies_attempted=["RATIO_CONSISTENCY", "CONSTANT_WITHIN_GROUP"])
    assert result_1.candidates == result_2.candidates
    assert result_1.candidates[0].strategy == "CONSTANT_WITHIN_GROUP"  # alphabetically first

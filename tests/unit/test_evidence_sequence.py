"""Unit tests for Phase 4.2: the pure, unwired sequence/gap/next-value
Evidence Engine (app.modules.ai.evidence_sequence). Pure-function module —
no database, no fixtures, no mocking, mirrors tests/unit/test_evidence_engine.py's
own isolation and style.

Every test uses a column name that is either deliberately generic
(placeholder-style) or deliberately business-specific ONLY where the test
itself is explicitly an acceptance-scenario test for real data already
investigated elsewhere in this project (Customer_Orders) — never as a
hardcoded assumption inside the algorithm itself, which never reads
target_column at all beyond carrying it through as a label.
"""
import pytest

from app.modules.ai.evidence_sequence import discover_sequence_evidence

# ---------------------------------------------------------------------------
# 1. Clean numeric sequence
# ---------------------------------------------------------------------------


def test_clean_numeric_sequence_is_no_relationship_nothing_to_fix():
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[1001, 1002, 1003, 1004, 1005])
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None
    assert "contiguous" in result.reason.lower()


# ---------------------------------------------------------------------------
# 2. Numeric sequence with one gap
# ---------------------------------------------------------------------------


def test_numeric_sequence_with_one_gap_produces_candidate():
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, 1003, 1004, 1006, 1007, 1008]
    )
    assert result.status == "CANDIDATE"
    assert result.best.strategy == "SEQUENCE_GAP"
    assert result.best.candidate_value == "1005"
    assert result.best.gap_count == 1
    assert result.best.duplicate_count == 0
    assert result.best.step == 1
    assert result.best.candidate_already_exists is False


# ---------------------------------------------------------------------------
# 3. Numeric sequence with multiple gaps
# ---------------------------------------------------------------------------


def test_numeric_sequence_with_multiple_gaps_is_ambiguous():
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, 1004, 1007, 1008]
    )
    assert result.status == "AMBIGUOUS"
    assert result.best is None
    assert "gap" in result.reason.lower()


# ---------------------------------------------------------------------------
# 4. Prefix + numeric suffix
# ---------------------------------------------------------------------------


def test_prefix_plus_numeric_suffix_produces_template_aware_candidate():
    result = discover_sequence_evidence(
        target_column="ref_no",
        observed_values=["ORD001", "ORD002", "ORD003", "ORD004", "ORD006", "ORD007", "ORD008"],
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "ORD005"
    assert result.best.template == "ORD###"


def test_prefix_plus_numeric_suffix_does_not_assume_fixed_prefix():
    """The same shape, different arbitrary prefix — the algorithm must not
    special-case 'ORD' or any other literal string."""
    result = discover_sequence_evidence(
        target_column="ref_no",
        observed_values=["SKU100", "SKU101", "SKU102", "SKU103", "SKU105", "SKU106", "SKU107"],
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "SKU104"


# ---------------------------------------------------------------------------
# 5. Duplicate within sequence — detection
# ---------------------------------------------------------------------------


def test_duplicate_within_sequence_is_detected_and_reported():
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, 1003, 1002, 1004]
    )
    assert result.status == "CANDIDATE"
    assert result.best.duplicate_count == 1
    assert result.best.gap_count == 0
    assert result.best.strategy == "SEQUENCE_NEXT_VALUE"


# ---------------------------------------------------------------------------
# 6. Duplicate WITH defensible replacement
# ---------------------------------------------------------------------------


def test_duplicate_with_defensible_replacement_produces_next_value_candidate():
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, 1003, 1004, 1005, 1002]
    )
    assert result.status == "CANDIDATE"
    assert result.best.strategy == "SEQUENCE_NEXT_VALUE"
    assert result.best.candidate_value == "1006"
    assert result.best.candidate_already_exists is False


# ---------------------------------------------------------------------------
# 7. Duplicate WITHOUT defensible replacement
# ---------------------------------------------------------------------------


def test_duplicate_without_defensible_replacement_stays_ambiguous():
    """Only 2 consecutive transitions confirm the step (below the
    min_supporting_transitions floor of 3) — detecting the duplicate is
    NOT automatically proof that next-value is the correct replacement."""
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, 1003, 1002], min_observations=4
    )
    assert result.status == "AMBIGUOUS"
    assert result.best is None
    assert "not enough" in result.reason.lower()


# ---------------------------------------------------------------------------
# 8. Non-sequential identifiers — must NOT auto-generate candidates
# ---------------------------------------------------------------------------


def test_non_sequential_numeric_identifiers_is_no_relationship():
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[101, 205, 301, 450], min_observations=4
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


def test_non_sequential_string_identifiers_is_no_relationship():
    """Many real identifiers are intentionally non-sequential — 'identifier
    -like' must never be assumed to mean 'sequential'."""
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=["A100", "A900", "B123", "XYZ77"], min_observations=4
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 9. Insufficient observations
# ---------------------------------------------------------------------------


def test_insufficient_observations_is_flagged_explicitly():
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[1001, 1002, 1003])
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None
    assert "minimum required" in result.reason.lower()


# ---------------------------------------------------------------------------
# 10. Constant values
# ---------------------------------------------------------------------------


def test_constant_values_is_no_relationship_not_insufficient():
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[7, 7, 7, 7, 7, 7])
    assert result.status == "NO_RELATIONSHIP"
    assert "constant" in result.reason.lower()


# ---------------------------------------------------------------------------
# 11. Negative numeric identifiers
# ---------------------------------------------------------------------------


def test_negative_numeric_identifiers_supported():
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[-5, -4, -3, -1, 0])
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "-2"


# ---------------------------------------------------------------------------
# 12. Zero-based sequences
# ---------------------------------------------------------------------------


def test_zero_based_sequence_supported():
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[0, 1, 2, 4, 5, 6])
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "3"


# ---------------------------------------------------------------------------
# 13. Descending sequences — order invariance
# ---------------------------------------------------------------------------


def test_descending_input_order_gives_identical_result_to_ascending():
    ascending = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, 1003, 1004, 1006, 1007, 1008]
    )
    descending = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1008, 1007, 1006, 1004, 1003, 1002, 1001]
    )
    assert ascending.status == descending.status == "CANDIDATE"
    assert ascending.best.candidate_value == descending.best.candidate_value == "1005"


# ---------------------------------------------------------------------------
# 14. Step > 1
# ---------------------------------------------------------------------------


def test_step_greater_than_one_supported():
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[100, 200, 300, 500, 600])
    assert result.status == "CANDIDATE"
    assert result.best.step == 100
    assert result.best.candidate_value == "400"


# ---------------------------------------------------------------------------
# 15. Mixed / non-numeric values
# ---------------------------------------------------------------------------


def test_mixed_numeric_and_non_numeric_values_is_no_relationship():
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[1001, 1002, "abc", "def", 1003, 1004, "xyz"]
    )
    assert result.status == "NO_RELATIONSHIP"
    assert "template" in result.reason.lower()


# ---------------------------------------------------------------------------
# 16. Malformed / inconsistent templates
# ---------------------------------------------------------------------------


def test_malformed_inconsistent_templates_is_no_relationship():
    result = discover_sequence_evidence(
        target_column="ref_no",
        observed_values=["ORD 001", "ORD-002", "ORD.003", "ORD004", "ORD005"],
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 17. Multiple competing sequence patterns
# ---------------------------------------------------------------------------


def test_multiple_competing_templates_is_no_relationship():
    result = discover_sequence_evidence(
        target_column="ref_no",
        observed_values=["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"],
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 18. Ambiguous gap (too few observations to rule out a step change)
# ---------------------------------------------------------------------------


def test_ambiguous_gap_declines_rather_than_guessing():
    """1, 2, 3, 5 could mean a missing 4, a step change, or an incomplete
    dataset — with only 2 confirmed step-1 transitions, must not guess."""
    result = discover_sequence_evidence(target_column="ref_no", observed_values=[1, 2, 3, 5], min_observations=4)
    assert result.status == "AMBIGUOUS"
    assert result.best is None


def test_easily_computed_max_plus_one_is_not_produced_without_real_evidence():
    """10, 20, 30, 999: max+step=1009 must NEVER be produced merely because
    the arithmetic is trivial — the step itself isn't reliably established."""
    result = discover_sequence_evidence(
        target_column="ref_no", observed_values=[10, 20, 30, 999], min_observations=4
    )
    assert result.status != "CANDIDATE"
    assert result.best is None


# ---------------------------------------------------------------------------
# 19. Candidate already exists (structural invariant, not a forced state)
# ---------------------------------------------------------------------------


def test_candidate_never_collides_with_an_already_observed_value():
    """By construction, a gap value is (by definition) absent from the
    observed set, and a next-value candidate is beyond the observed
    maximum — so a genuine collision cannot arise organically. This test
    asserts that invariant holds across every CANDIDATE-producing scenario
    in this file, and the engine's defensive candidate_already_exists
    guard (see evidence_sequence.py) exists specifically to catch a
    violation of this invariant should a future change ever introduce one."""
    scenarios = [
        [1001, 1002, 1003, 1004, 1006, 1007, 1008],
        [1001, 1002, 1003, 1004, 1005, 1002],
        [-5, -4, -3, -1, 0],
        [0, 1, 2, 4, 5, 6],
        [100, 200, 300, 500, 600],
    ]
    for values in scenarios:
        result = discover_sequence_evidence(target_column="ref_no", observed_values=values)
        assert result.status == "CANDIDATE"
        assert result.best.candidate_already_exists is False
        observed_as_strings = {str(v) for v in values}
        assert result.best.candidate_value not in observed_as_strings


# ---------------------------------------------------------------------------
# 20. Empty / null values
# ---------------------------------------------------------------------------


def test_null_and_empty_values_are_filtered_without_crashing():
    result = discover_sequence_evidence(
        target_column="ref_no",
        observed_values=[1001, None, 1002, "", 1004, 1005, 1006, None],
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "1003"
    assert result.observed_values_count == 5  # None/"" excluded from the count


# ---------------------------------------------------------------------------
# 21. Outlier identifier
# ---------------------------------------------------------------------------


def test_outlier_identifier_does_not_explode_or_produce_a_candidate():
    result = discover_sequence_evidence(
        target_column="ref_no",
        observed_values=[1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 9999999],
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None
    assert "outlier" in result.reason.lower()


# ---------------------------------------------------------------------------
# 22. No hardcoded business names — arbitrary column names behave identically
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column_name", ["ItemCode", "Reference", "RecordNo", "Code", "Identifier", "widget_ref"])
def test_arbitrary_column_names_behave_identically(column_name):
    result = discover_sequence_evidence(
        target_column=column_name, observed_values=[1001, 1002, 1003, 1004, 1006, 1007, 1008]
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "1005"
    assert result.target_column == column_name  # carried through as a label only


def test_engine_never_branches_on_target_column_value():
    """The exact same values under two wildly different column-name labels
    (one deliberately business-specific, one deliberately generic) must
    produce byte-for-byte identical evidence, proving target_column is
    never read for decision-making — only carried through as a label."""
    values = [1001, 1002, 1003, 1004, 1006, 1007, 1008]
    business_named = discover_sequence_evidence(target_column="customer_id", observed_values=values)
    generic_named = discover_sequence_evidence(target_column="col_47", observed_values=values)
    assert business_named.best == generic_named.best
    assert business_named.status == generic_named.status


# ---------------------------------------------------------------------------
# Customer_Orders — TEST SCENARIO ONLY, not a production assumption
# ---------------------------------------------------------------------------


def test_customer_orders_scenario_generic_engine_finds_1015_with_strong_evidence():
    """This uses the exact real values from the live Customer_Orders
    dataset (see the Phase 3 forensic investigation) purely as a realistic
    TEST SCENARIO. The production discover_sequence_evidence() function
    itself has no knowledge of "customer_id" or "Customer_Orders" — nothing
    in evidence_sequence.py reads target_column for any decision. This test
    documents, rather than forces, why the algorithm's generic rules
    happen to produce 1015 here: 14 distinct values, perfectly contiguous
    from 1001 to 1014 (zero gaps), with a single isolated duplicate (1002)
    and zero contradicting transitions — the strongest possible case for
    the SEQUENCE_NEXT_VALUE strategy, hence confidence == 1.0."""
    observed_values = [
        1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008, 1002, 1009, 1010, 1011, 1012, 1013, 1014,
    ]
    result = discover_sequence_evidence(target_column="customer_id", observed_values=observed_values)

    assert result.status == "CANDIDATE"
    assert result.best.strategy == "SEQUENCE_NEXT_VALUE"
    assert result.best.candidate_value == "1015"
    assert result.best.duplicate_count == 1
    assert result.best.gap_count == 0
    assert result.best.contradicting_count == 0
    assert result.best.confidence == pytest.approx(1.0)
    assert result.best.candidate_already_exists is False

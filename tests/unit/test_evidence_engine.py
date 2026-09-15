"""Unit tests for the Phase 0 Evidence Engine (app.modules.ai.evidence).

Pure-function module — no database, no fixtures, no mocking needed. Every
test constructs plain dict rows and ColumnMeta directly, matching the
module's own independence from the rest of the application.
"""
import math

import pytest

from app.modules.ai.evidence import (
    ColumnMeta,
    ColumnRole,
    classify_column_role,
    classify_columns,
    discover_relationship_evidence,
)

# ---------------------------------------------------------------------------
# Column role classification
# ---------------------------------------------------------------------------


def test_primary_key_is_always_identifier_regardless_of_type_or_cardinality():
    meta = ColumnMeta(name="id", normalized_data_type="INTEGER", is_primary_key=True, distinct_percentage=10.0)
    assert classify_column_role(meta) == ColumnRole.IDENTIFIER


def test_small_sample_all_distinct_numeric_column_stays_measure_not_identifier():
    """The exact trap this design deliberately avoids: Qty=[10,20,5,15] is
    100% distinct in a 4-row sample purely by chance — must not be promoted
    to IDENTIFIER just because every value happens to differ."""
    rows = [{"Qty": 10}, {"Qty": 20}, {"Qty": 5}, {"Qty": 15}]
    meta = ColumnMeta(name="Qty", normalized_data_type="INTEGER")
    assert classify_column_role(meta, sample_rows=rows) == ColumnRole.MEASURE


def test_large_sample_all_distinct_numeric_column_promotes_to_identifier():
    rows = [{"code": i} for i in range(25)]
    meta = ColumnMeta(name="code", normalized_data_type="INTEGER")
    assert classify_column_role(meta, sample_rows=rows) == ColumnRole.IDENTIFIER


def test_small_sample_all_distinct_string_column_promotes_to_identifier():
    """The opposite asymmetry: an all-unique STRING column (e.g. employee
    names) is a strong identifier signal even with very few rows — a
    categorical dimension would almost always repeat instead."""
    rows = [{"employee": n} for n in ["Alice", "Bob", "Carol", "Dave", "Eve"]]
    meta = ColumnMeta(name="employee", normalized_data_type="STRING")
    assert classify_column_role(meta, sample_rows=rows) == ColumnRole.IDENTIFIER


def test_low_cardinality_string_column_is_dimension():
    rows = [{"product": "Apple"}] * 3 + [{"product": "Banana"}]
    meta = ColumnMeta(name="product", normalized_data_type="STRING")
    assert classify_column_role(meta, sample_rows=rows) == ColumnRole.DIMENSION


def test_date_column_is_other():
    meta = ColumnMeta(name="order_date", normalized_data_type="DATE")
    assert classify_column_role(meta) == ColumnRole.OTHER


def test_date_time_and_attribute_roles_exist_but_are_not_yet_produced():
    """Phase 4.1 foundation: DATE_TIME/ATTRIBUTE are now real ColumnRole
    members (for later Phase 4 sub-phases to target), but
    classify_column_role() must not return either of them yet — DATE
    columns still classify as OTHER (see test_date_column_is_other above),
    unchanged from before this phase."""
    assert ColumnRole.DATE_TIME.value == "DATE_TIME"
    assert ColumnRole.ATTRIBUTE.value == "ATTRIBUTE"
    assert ColumnRole.DATE_TIME not in {
        classify_column_role(ColumnMeta(name="order_date", normalized_data_type="DATE")),
        classify_column_role(ColumnMeta(name="order_date", normalized_data_type="DATETIME")),
    }


def test_dataset_wide_profile_distinct_percentage_is_preferred_over_sample_derived():
    # Sample looks 100% distinct, but the caller supplies a real, large-sample
    # profiling stat saying it's actually low-cardinality -> must win.
    rows = [{"status": "A"}, {"status": "B"}, {"status": "C"}]
    meta = ColumnMeta(name="status", normalized_data_type="STRING", distinct_percentage=2.0)
    assert classify_column_role(meta, sample_rows=rows) == ColumnRole.DIMENSION


def test_classify_columns_batches_all_columns():
    columns = [
        ColumnMeta(name="Product", normalized_data_type="STRING"),
        ColumnMeta(name="Qty", normalized_data_type="INTEGER"),
    ]
    rows = [{"Product": "Apple", "Qty": 1}, {"Product": "Apple", "Qty": 2}]
    roles = classify_columns(columns, sample_rows=rows)
    assert roles == {"Product": ColumnRole.DIMENSION, "Qty": ColumnRole.MEASURE}


# ---------------------------------------------------------------------------
# The exact target scenario from the design report
# ---------------------------------------------------------------------------


def _product_qty_amount_rows():
    return [
        {"Product": "Apple", "Qty": 10, "OrderAmount": 490},
        {"Product": "Apple", "Qty": 20, "OrderAmount": 980},
        {"Product": "Apple", "Qty": 5, "OrderAmount": 245},
        {"Product": "Apple", "Qty": 15, "OrderAmount": -500},
    ]


def _product_qty_amount_columns():
    return [
        ColumnMeta(name="Product", normalized_data_type="STRING"),
        ColumnMeta(name="Qty", normalized_data_type="INTEGER"),
        ColumnMeta(name="OrderAmount", normalized_data_type="DECIMAL"),
    ]


def test_target_scenario_discovers_ratio_consistency_candidate_735():
    rows = _product_qty_amount_rows()
    failing_row = rows[3]
    columns = _product_qty_amount_columns()

    roles = classify_columns(columns, sample_rows=rows)
    assert roles["Product"] == ColumnRole.DIMENSION
    assert roles["Qty"] == ColumnRole.MEASURE
    assert roles["OrderAmount"] == ColumnRole.MEASURE

    result = discover_relationship_evidence(
        target_column="OrderAmount", failing_row=failing_row, candidate_rows=rows, columns=columns,
    )

    assert result.status == "CANDIDATE"
    assert result.comparable_group_size == 3  # the 3 clean Apple rows, failing row excluded
    assert result.best.relationship_type == "ratio_consistency"
    assert result.best.related_columns == ("Qty",)
    assert result.best.candidate_value == pytest.approx(735.0)
    assert result.best.fit_quality == pytest.approx(1.0)
    assert result.best.coefficient_of_variation == pytest.approx(0.0)


def test_target_scenario_is_deterministic_and_reproducible():
    rows = _product_qty_amount_rows()
    columns = _product_qty_amount_columns()
    results = [
        discover_relationship_evidence(
            target_column="OrderAmount", failing_row=rows[3], candidate_rows=rows, columns=columns
        )
        for _ in range(5)
    ]
    values = {r.best.candidate_value for r in results}
    assert values == {735.0}


def test_target_scenario_failing_rows_own_target_value_never_influences_result():
    """Changing the failing row's OWN target value (the thing being
    replaced) must not change the discovered candidate at all — only its
    other column values (Qty) may."""
    rows = _product_qty_amount_rows()
    columns = _product_qty_amount_columns()
    mutated = dict(rows[3])
    mutated["OrderAmount"] = -999999  # wildly different "bad" value
    rows_mutated = rows[:3] + [mutated]

    result = discover_relationship_evidence(
        target_column="OrderAmount", failing_row=mutated, candidate_rows=rows_mutated, columns=columns
    )
    assert result.best.candidate_value == pytest.approx(735.0)


# ---------------------------------------------------------------------------
# Employee / Salary / Tax — whole-dataset ratio, no dimension column
# ---------------------------------------------------------------------------


def test_employee_salary_tax_whole_dataset_ratio_no_dimension_column():
    rows = [
        {"Employee": "Alice", "Salary": 1000, "Tax": 200},
        {"Employee": "Bob", "Salary": 2000, "Tax": 400},
        {"Employee": "Carol", "Salary": 1500, "Tax": 300},
        {"Employee": "Dave", "Salary": 4000, "Tax": -50},  # failing Tax value
    ]
    failing_row = rows[3]
    columns = [
        ColumnMeta(name="Employee", normalized_data_type="STRING"),
        ColumnMeta(name="Salary", normalized_data_type="DECIMAL"),
        ColumnMeta(name="Tax", normalized_data_type="DECIMAL"),
    ]

    roles = classify_columns(columns, sample_rows=rows)
    # Employee is all-unique -> IDENTIFIER, not DIMENSION — so grouping falls
    # back to the whole dataset instead of a degenerate per-employee group.
    assert roles["Employee"] == ColumnRole.IDENTIFIER
    assert roles["Salary"] == ColumnRole.MEASURE
    assert roles["Tax"] == ColumnRole.MEASURE

    result = discover_relationship_evidence(
        target_column="Tax", failing_row=failing_row, candidate_rows=rows, columns=columns
    )

    assert result.status == "CANDIDATE"
    assert result.comparable_group_size == 3  # Alice/Bob/Carol — whole dataset minus the failing row
    assert result.best.relationship_type == "ratio_consistency"
    assert result.best.candidate_value == pytest.approx(800.0)  # 0.2 * 4000


# ---------------------------------------------------------------------------
# Invoice / Qty / UnitPrice / Discount / Total — discount breaks the simple
# Qty*UnitPrice model; must NOT produce a confident candidate.
# ---------------------------------------------------------------------------


def test_invoice_discount_breaks_simple_product_relationship_no_confident_candidate():
    rows = [
        {"Invoice": "INV-1", "Qty": 10, "UnitPrice": 5, "Discount": 0.0, "Total": 50},
        {"Invoice": "INV-2", "Qty": 20, "UnitPrice": 5, "Discount": 0.0, "Total": 100},
        {"Invoice": "INV-3", "Qty": 10, "UnitPrice": 5, "Discount": 0.5, "Total": 25},
        {"Invoice": "INV-4", "Qty": 10, "UnitPrice": 5, "Discount": 0.2, "Total": 40},
    ]
    failing_row = {"Invoice": "INV-5", "Qty": 10, "UnitPrice": 5, "Discount": 0.3, "Total": -1}
    columns = [
        ColumnMeta(name="Invoice", normalized_data_type="STRING", is_primary_key=True),
        ColumnMeta(name="Qty", normalized_data_type="INTEGER"),
        ColumnMeta(name="UnitPrice", normalized_data_type="DECIMAL"),
        ColumnMeta(name="Discount", normalized_data_type="DECIMAL"),
        ColumnMeta(name="Total", normalized_data_type="DECIMAL"),
    ]

    result = discover_relationship_evidence(
        target_column="Total", failing_row=failing_row, candidate_rows=rows, columns=columns,
        min_fit_quality=0.9,
    )

    # The naive Qty*UnitPrice model (and every other 2-variable shape) fails
    # to clear the confidence bar once discounted rows are mixed in.
    assert result.status in ("NO_RELATIONSHIP", "AMBIGUOUS")
    assert result.best is None
    # But every shape that WAS tried is preserved for diagnostics, proving
    # this isn't "gave up without looking" — it genuinely tried and declined.
    tried_types = {c.relationship_type for c in result.rejected_alternatives}
    assert "product_consistency" in tried_types
    weakest_product = next(c for c in result.rejected_alternatives if c.relationship_type == "product_consistency")
    assert weakest_product.fit_quality < 0.9


# ---------------------------------------------------------------------------
# No meaningful relationship at all
# ---------------------------------------------------------------------------


def test_no_meaningful_relationship_returns_no_relationship():
    rows = [
        {"Category": "X", "A": 3, "B": 17, "Target": 91},
        {"Category": "X", "A": 42, "B": 2, "Target": 5},
        {"Category": "X", "A": 8, "B": 200, "Target": -13},
        {"Category": "X", "A": 71, "B": 9, "Target": 1000},
    ]
    failing_row = {"Category": "X", "A": 5, "B": 5, "Target": -1}
    columns = [
        ColumnMeta(name="Category", normalized_data_type="STRING"),
        ColumnMeta(name="A", normalized_data_type="INTEGER"),
        ColumnMeta(name="B", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]

    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns
    )
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# Tiny comparable group — perfect fit must still be rejected
# ---------------------------------------------------------------------------


def test_tiny_group_rejected_even_with_perfect_fit():
    rows = [
        {"Cat": "X", "M": 10, "Target": 100},
        {"Cat": "X", "M": 20, "Target": 200},
    ]
    failing_row = {"Cat": "X", "M": 5, "Target": -1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]

    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns,
        min_group_size=3,
    )
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None
    assert result.comparable_group_size == 2


# ---------------------------------------------------------------------------
# Multiple competing relationships -> ambiguity must be surfaced
# ---------------------------------------------------------------------------


def test_competing_relationships_are_marked_ambiguous_not_silently_resolved():
    # target is both ~constant (~100) AND target/M1 is ~constant (~2) —
    # these agree closely on fit quality but, for this failing row's M1=70,
    # disagree meaningfully on the resulting candidate value (140 vs ~100).
    rows = [
        {"Cat": "X", "M1": 50, "Target": 100},
        {"Cat": "X", "M1": 51, "Target": 102},
        {"Cat": "X", "M1": 49, "Target": 98},
    ]
    failing_row = {"Cat": "X", "M1": 70, "Target": -1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M1", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]

    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns,
        min_group_size=3, min_fit_quality=0.9, ambiguity_margin=0.05,
    )
    assert result.status == "AMBIGUOUS"
    assert result.best is None
    types_considered = {c.relationship_type for c in result.rejected_alternatives}
    assert {"constant_within_group", "ratio_consistency"} <= types_considered


# ---------------------------------------------------------------------------
# Null / non-numeric / zero / negative edge cases
# ---------------------------------------------------------------------------


def test_null_and_non_numeric_values_are_filtered_not_crashed_on():
    rows = [
        {"Cat": "X", "M": 10, "Target": 100},
        {"Cat": "X", "M": None, "Target": 200},  # null M -> excluded from ratio pairs
        {"Cat": "X", "M": "not-a-number", "Target": 300},  # garbage -> excluded
        {"Cat": "X", "M": 20, "Target": 200},
        {"Cat": "X", "M": 30, "Target": 300},
    ]
    failing_row = {"Cat": "X", "M": 15, "Target": -1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]

    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns, min_group_size=3,
    )
    # Must not raise; must still find the real ratio=10 relationship from
    # the 3 clean rows (10/100, 20/200, 30/300).
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == pytest.approx(150.0)
    assert result.best.comparable_group_size == 3


def test_zero_denominator_rows_excluded_from_ratio_not_crashing():
    rows = [
        {"Cat": "X", "M": 0, "Target": 500},  # zero M -> would divide by zero if not guarded
        {"Cat": "X", "M": 10, "Target": 100},
        {"Cat": "X", "M": 20, "Target": 200},
        {"Cat": "X", "M": 30, "Target": 300},
    ]
    failing_row = {"Cat": "X", "M": 5, "Target": -1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]
    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns, min_group_size=3,
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == pytest.approx(50.0)


def test_negative_values_work_arithmetically_without_special_casing():
    rows = [
        {"Cat": "X", "M": -10, "Target": -100},
        {"Cat": "X", "M": -20, "Target": -200},
        {"Cat": "X", "M": -30, "Target": -300},
    ]
    failing_row = {"Cat": "X", "M": -5, "Target": 1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]
    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns, min_group_size=3,
    )
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == pytest.approx(-50.0)


def test_result_never_contains_nan_or_infinite_candidate():
    rows = [
        {"Cat": "X", "M": 1e300, "Target": 1e300},
        {"Cat": "X", "M": 2e300, "Target": 2e300},
        {"Cat": "X", "M": 3e300, "Target": 3e300},
    ]
    failing_row = {"Cat": "X", "M": 1e300, "Target": -1}  # * itself would overflow to inf for product-type math
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="DECIMAL"),
        ColumnMeta(name="Target", normalized_data_type="DECIMAL"),
    ]
    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns, min_group_size=3,
    )
    if result.best is not None:
        assert math.isfinite(result.best.candidate_value)
    for c in result.rejected_alternatives:
        assert math.isfinite(c.candidate_value)


# ---------------------------------------------------------------------------
# Outlier rows must not establish a false relationship
# ---------------------------------------------------------------------------


def test_outlier_in_comparable_group_prevents_confident_candidate():
    rows = [
        {"Cat": "X", "M": 10, "Target": 100},  # ratio 10
        {"Cat": "X", "M": 20, "Target": 200},  # ratio 10
        {"Cat": "X", "M": 30, "Target": 300},  # ratio 10
        {"Cat": "X", "M": 5, "Target": 5000},  # wild outlier, ratio 1000
    ]
    failing_row = {"Cat": "X", "M": 40, "Target": -1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]
    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns,
        min_group_size=3, min_fit_quality=0.9,
    )
    # The outlier drags the coefficient of variation up enough that the
    # otherwise-perfect ratio=10 pattern no longer clears the confidence bar.
    assert result.status == "NO_RELATIONSHIP"
    ratio_candidates = [c for c in result.rejected_alternatives if c.relationship_type == "ratio_consistency"]
    assert ratio_candidates and ratio_candidates[0].fit_quality < 0.9


# ---------------------------------------------------------------------------
# Confidence discount by group size
# ---------------------------------------------------------------------------


def test_confidence_is_more_conservative_than_fit_quality_for_small_groups():
    rows = [
        {"Cat": "X", "M": 10, "Target": 100},
        {"Cat": "X", "M": 20, "Target": 200},
        {"Cat": "X", "M": 30, "Target": 300},
    ]
    failing_row = {"Cat": "X", "M": 40, "Target": -1}
    columns = [
        ColumnMeta(name="Cat", normalized_data_type="STRING"),
        ColumnMeta(name="M", normalized_data_type="INTEGER"),
        ColumnMeta(name="Target", normalized_data_type="INTEGER"),
    ]
    result = discover_relationship_evidence(
        target_column="Target", failing_row=failing_row, candidate_rows=rows, columns=columns, min_group_size=3,
    )
    assert result.status == "CANDIDATE"
    assert result.best.fit_quality == pytest.approx(1.0)
    assert result.best.confidence < result.best.fit_quality  # group size == min_group_size -> discounted

from app.modules.validation.engine import (
    evaluate_completeness,
    evaluate_cross_column,
    evaluate_duplicate,
    evaluate_pattern,
    evaluate_range,
    evaluate_uniqueness,
    get_evaluator,
    is_supported_rule_type,
)
from app.source_adapters.base import ColumnExactStats


def test_completeness_flags_null_rows_when_sample_derived() -> None:
    rows = [{"email": "a@x.com"}, {"email": None}, {"email": "b@x.com"}]

    failures = evaluate_completeness(
        rows=rows, column_name="email", definition={"max_null_percentage": 0}, exact_stats=None
    )

    assert set(failures.keys()) == {1}
    assert failures[1].failed_value is None


def test_completeness_uses_exact_stats_to_skip_evaluation_when_within_threshold() -> None:
    rows = [{"email": None}]  # would fail if sample-derived
    exact_stats = ColumnExactStats(null_count=1, distinct_count=0, total_row_count=1000)  # 0.1% null

    failures = evaluate_completeness(
        rows=rows, column_name="email", definition={"max_null_percentage": 5.0}, exact_stats=exact_stats
    )

    assert failures == {}


def test_uniqueness_flags_all_occurrences_of_a_duplicated_value() -> None:
    rows = [{"code": "A"}, {"code": "B"}, {"code": "A"}]

    failures = evaluate_uniqueness(
        rows=rows, column_name="code", definition={"max_duplicate_percentage": 0}, exact_stats=None
    )

    assert set(failures.keys()) == {0, 2}


def test_duplicate_flags_full_row_duplicates_only() -> None:
    rows = [{"a": 1, "b": "x"}, {"a": 1, "b": "y"}, {"a": 1, "b": "x"}]

    failures = evaluate_duplicate(rows=rows, definition={})

    assert set(failures.keys()) == {0, 2}


def test_range_flags_out_of_bounds_values() -> None:
    rows = [{"age": 5}, {"age": 40}, {"age": 200}]

    failures = evaluate_range(rows=rows, column_name="age", definition={"min": 0, "max": 120})

    assert set(failures.keys()) == {2}


def test_range_ignores_nulls_and_non_numeric() -> None:
    rows = [{"age": None}, {"age": "not-a-number"}]

    failures = evaluate_range(rows=rows, column_name="age", definition={"min": 0, "max": 120})

    assert failures == {}


def test_pattern_flags_non_matching_values() -> None:
    rows = [{"code": "AB-123"}, {"code": "bad"}]

    failures = evaluate_pattern(rows=rows, column_name="code", definition={"regex": r"^[A-Z]{2}-\d{3}$"})

    assert set(failures.keys()) == {1}


def test_cross_column_all_equal_flags_mismatches() -> None:
    rows = [{"a": 1, "b": 1}, {"a": 1, "b": 2}]

    failures = evaluate_cross_column(rows=rows, column_names_in_order=["a", "b"], definition={"check": "all_equal"})

    assert set(failures.keys()) == {1}


def test_referential_integrity_is_not_a_supported_rule_type() -> None:
    assert is_supported_rule_type("REFERENTIAL_INTEGRITY") is False
    assert is_supported_rule_type("COMPLETENESS") is True


def test_get_evaluator_returns_callable_for_each_of_the_six_approved_types() -> None:
    for rule_type in ("COMPLETENESS", "UNIQUENESS", "DUPLICATE", "RANGE", "PATTERN", "CROSS_COLUMN"):
        assert callable(get_evaluator(rule_type))

from app.modules.validation.staged_revalidation import (
    STATUS_REQUIRES_DATASET_REVALIDATION,
    STATUS_REVALIDATED_FAIL,
    STATUS_REVALIDATED_PASS,
    revalidate_row_against_rule,
)


def test_pattern_fail_on_corrected_row_becomes_pass():
    row = {"email": "suresh.kumar@gmail.com"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        column_name="email",
    )
    assert result.status == STATUS_REVALIDATED_PASS
    assert result.checked_value == "suresh.kumar@gmail.com"


def test_pattern_still_invalid_after_edit_stays_fail():
    row = {"email": "still-invalid"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        column_name="email",
    )
    assert result.status == STATUS_REVALIDATED_FAIL
    assert result.checked_value == "still-invalid"
    assert result.reason is not None


def test_completeness_null_becomes_pass_after_correction():
    row = {"phone": "555-1234"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_name="phone",
    )
    assert result.status == STATUS_REVALIDATED_PASS


def test_completeness_still_null_stays_fail():
    row = {"phone": None}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_name="phone",
    )
    assert result.status == STATUS_REVALIDATED_FAIL


def test_range_invalid_becomes_pass_after_correction():
    row = {"amount": "500"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="RANGE", definition={"min": 0, "max": 1000}, column_name="amount",
    )
    assert result.status == STATUS_REVALIDATED_PASS


def test_range_still_invalid_stays_fail():
    row = {"amount": "-500"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="RANGE", definition={"min": 0, "max": 1000}, column_name="amount",
    )
    assert result.status == STATUS_REVALIDATED_FAIL
    assert result.checked_value == "-500"


def test_cross_column_all_equal_row_local_pass():
    row = {"a": "x", "b": "x"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="CROSS_COLUMN", definition={"check": "all_equal"},
        column_names_in_order=["a", "b"],
    )
    assert result.status == STATUS_REVALIDATED_PASS


def test_cross_column_all_equal_row_local_fail():
    row = {"a": "x", "b": "y"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="CROSS_COLUMN", definition={"check": "all_equal"},
        column_names_in_order=["a", "b"],
    )
    assert result.status == STATUS_REVALIDATED_FAIL


def test_uniqueness_is_never_falsely_row_local_passed():
    """A single row can never collide with itself — evaluate_uniqueness
    would trivially 'pass' if actually invoked on rows=[row]. Must
    instead report REQUIRES_DATASET_REVALIDATION, never PASS."""
    row = {"code": "ABC123"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="UNIQUENESS", definition={"max_duplicate_percentage": 0}, column_name="code",
    )
    assert result.status == STATUS_REQUIRES_DATASET_REVALIDATION
    assert result.status != STATUS_REVALIDATED_PASS


def test_duplicate_is_never_falsely_row_local_passed():
    row = {"a": "x", "b": "y"}
    result = revalidate_row_against_rule(row_snapshot=row, rule_type="DUPLICATE", definition={})
    assert result.status == STATUS_REQUIRES_DATASET_REVALIDATION
    assert result.status != STATUS_REVALIDATED_PASS


def test_unsupported_rule_type_requires_dataset_revalidation():
    row = {"x": 1}
    result = revalidate_row_against_rule(row_snapshot=row, rule_type="REFERENTIAL_INTEGRITY", definition={})
    assert result.status == STATUS_REQUIRES_DATASET_REVALIDATION


def test_never_raises_for_missing_column_in_snapshot():
    row = {"other": "value"}
    result = revalidate_row_against_rule(
        row_snapshot=row, rule_type="PATTERN", definition={"regex": r"^\d+$"}, column_name="missing_column",
    )
    # missing -> None -> evaluator's own None-skip semantics -> no failure recorded
    assert result.status == STATUS_REVALIDATED_PASS
    assert result.checked_value is None

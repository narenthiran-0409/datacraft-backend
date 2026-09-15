from datetime import date, datetime
from decimal import Decimal

import pytest

from app.modules.staging.value_coercion import CorrectionCoercionError, coerce_final_value


def test_none_passes_through_regardless_of_type() -> None:
    for normalized_type in ("INTEGER", "DECIMAL", "BOOLEAN", "DATE", "DATETIME", "STRING", "TEXT"):
        assert coerce_final_value(None, normalized_type=normalized_type, column_name="x") is None


def test_integer_coercion() -> None:
    assert coerce_final_value("980", normalized_type="INTEGER", column_name="order_amount") == 980
    assert coerce_final_value(" -500 ", normalized_type="INTEGER", column_name="x") == -500


def test_integer_coercion_failure_raises() -> None:
    with pytest.raises(CorrectionCoercionError):
        coerce_final_value("not-a-number", normalized_type="INTEGER", column_name="order_amount")


def test_decimal_coercion() -> None:
    assert coerce_final_value("980.50", normalized_type="DECIMAL", column_name="x") == Decimal("980.50")


def test_decimal_coercion_failure_raises() -> None:
    with pytest.raises(CorrectionCoercionError):
        coerce_final_value("abc", normalized_type="DECIMAL", column_name="x")


@pytest.mark.parametrize("raw,expected", [("true", True), ("True", True), ("1", True), ("yes", True),
                                           ("false", False), ("0", False), ("no", False)])
def test_boolean_coercion(raw: str, expected: bool) -> None:
    assert coerce_final_value(raw, normalized_type="BOOLEAN", column_name="x") is expected


def test_boolean_coercion_failure_raises() -> None:
    with pytest.raises(CorrectionCoercionError):
        coerce_final_value("maybe", normalized_type="BOOLEAN", column_name="x")


def test_date_coercion() -> None:
    assert coerce_final_value("2024-01-15", normalized_type="DATE", column_name="x") == date(2024, 1, 15)


def test_date_coercion_failure_raises() -> None:
    with pytest.raises(CorrectionCoercionError):
        coerce_final_value("not-a-date", normalized_type="DATE", column_name="x")


def test_datetime_coercion_full_iso() -> None:
    assert coerce_final_value(
        "2024-01-15T10:30:00", normalized_type="DATETIME", column_name="x"
    ) == datetime(2024, 1, 15, 10, 30, 0)


def test_datetime_coercion_date_only_string() -> None:
    assert coerce_final_value(
        "2024-01-15", normalized_type="DATETIME", column_name="x"
    ) == datetime(2024, 1, 15, 0, 0, 0)


def test_datetime_coercion_failure_raises() -> None:
    with pytest.raises(CorrectionCoercionError):
        coerce_final_value("garbage", normalized_type="DATETIME", column_name="x")


def test_string_and_text_and_unrecognized_pass_through_unchanged() -> None:
    assert coerce_final_value("hello@example.com", normalized_type="STRING", column_name="x") == "hello@example.com"
    assert coerce_final_value("a long text", normalized_type="TEXT", column_name="x") == "a long text"
    assert coerce_final_value("raw", normalized_type="SOMETHING_ELSE", column_name="x") == "raw"

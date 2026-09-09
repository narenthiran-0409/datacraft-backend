from decimal import Decimal

from app.modules.profiling.engine import compute_dataset_duplicate_percentage, profile_column
from app.source_adapters.base import ColumnExactStats


def test_numeric_column_stats_with_sample_derived_fallback() -> None:
    values = [1, 2, 3, 4, 5, None]  # mean=3, stddev(pop)=sqrt(2)=1.414..., sum=15

    result = profile_column(
        column_name="score",
        normalized_data_type="INTEGER",
        sample_values=values,
        exact_stats=None,
        include_top_values=False,
    )

    assert result["null_count"] == 1
    assert result["distinct_count"] == 5
    assert float(result["mean_value"]) == 3.0
    assert float(result["sum_value"]) == 15.0
    assert round(float(result["stddev_value"]), 3) == round(1.4142135623730951, 3)
    assert result["pattern_summary"]["exact_stats"] is False


def test_numeric_column_uses_exact_stats_when_available() -> None:
    exact = ColumnExactStats(null_count=2, distinct_count=3, total_row_count=10)

    result = profile_column(
        column_name="score",
        normalized_data_type="INTEGER",
        sample_values=[1, 2, 3, 3, None],
        exact_stats=exact,
        include_top_values=False,
    )

    assert result["null_count"] == 2
    assert result["distinct_count"] == 3
    assert result["pattern_summary"]["exact_stats"] is True
    # duplicate_count derived from exact non-null (10-2=8) minus distinct (3) = 5
    assert result["duplicate_count"] == 5


def test_string_column_length_and_blank_stats() -> None:
    values = ["ab", "", "abcd", None, "  ", "z"]

    result = profile_column(
        column_name="name",
        normalized_data_type="STRING",
        sample_values=values,
        exact_stats=None,
        include_top_values=False,
    )

    assert result["min_length"] == 0
    assert result["max_length"] == 4
    # blank_count counts strings that are empty after strip(): "" and "  "
    assert result["pattern_summary"]["blank_count"] == 2


def test_date_column_min_max() -> None:
    from datetime import date

    values = [date(2024, 1, 1), date(2023, 5, 5), None, date(2025, 12, 31)]

    result = profile_column(
        column_name="created",
        normalized_data_type="DATE",
        sample_values=values,
        exact_stats=None,
        include_top_values=False,
    )

    assert result["min_value"] == str(date(2023, 5, 5))
    assert result["max_value"] == str(date(2025, 12, 31))


def test_top_values_off_by_default_means_none() -> None:
    result = profile_column(
        column_name="cat",
        normalized_data_type="STRING",
        sample_values=["a", "a", "b"],
        exact_stats=None,
        include_top_values=False,
    )
    assert result["value_distribution"] is None


def test_top_values_capped_at_ten_and_truncated() -> None:
    long_value = "x" * 500
    sample = [long_value] * 5 + [f"v{i}" for i in range(20)]  # 21 distinct values total

    result = profile_column(
        column_name="cat",
        normalized_data_type="STRING",
        sample_values=sample,
        exact_stats=None,
        include_top_values=True,
    )

    assert result["value_distribution"] is not None
    assert len(result["value_distribution"]) <= 10
    top_entry = result["value_distribution"][0]
    assert top_entry["value"] == "x" * 100  # truncated to ~100 chars
    assert len(top_entry["value"]) == 100
    assert top_entry["count"] == 5


def test_outlier_count_iqr() -> None:
    values = [10, 11, 12, 13, 14, 15, 1000]  # 1000 is a clear IQR outlier

    result = profile_column(
        column_name="v",
        normalized_data_type="INTEGER",
        sample_values=values,
        exact_stats=None,
        include_top_values=False,
    )

    assert result["outlier_count"] >= 1


def test_dataset_duplicate_percentage_matches_sample() -> None:
    rows = [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "x"},  # duplicate of row 0
        {"a": 2, "b": "y"},
        {"a": 3, "b": "z"},
    ]

    pct = compute_dataset_duplicate_percentage(rows)

    assert pct == Decimal("25.00")  # 1 duplicate out of 4 rows


def test_dataset_duplicate_percentage_empty_sample_is_none() -> None:
    assert compute_dataset_duplicate_percentage([]) is None

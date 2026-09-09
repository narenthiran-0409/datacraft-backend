"""Fully generic, provider-agnostic profiling engine. Operates only on the
uniform shapes the source_adapters layer produces (ColumnExactStats,
plain Python values pulled from a SampleResult's rows) — no provider-
specific code belongs here.

Deliberately does NOT compute or reference quality_score anywhere, and does
NOT compute a dataset-level null_percentage anywhere (both are reserved for
a later Validation/Rule Engine phase). The dataset-level
duplicate_percentage computed here (via pandas .duplicated() on the full
pulled sample) is the ONLY dataset-level statistic this engine produces.
"""
import math
import statistics
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from app.source_adapters.base import ColumnExactStats

MAX_TOP_VALUES = 10
MAX_TOP_VALUE_LENGTH = 100


def _round_decimal(value: float, places: int = 6) -> Decimal:
    return Decimal(str(round(value, places)))


def _truncate(value: str, max_len: int = MAX_TOP_VALUE_LENGTH) -> str:
    return value if len(value) <= max_len else value[:max_len]


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[int(f)] * (c - k) + sorted_values[int(c)] * (k - f)


def _iqr_outlier_count(values: list[float]) -> int:
    if len(values) < 4:
        return 0
    sorted_values = sorted(values)
    q1 = _percentile(sorted_values, 25)
    q3 = _percentile(sorted_values, 75)
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr
    return sum(1 for v in values if v < lower or v > upper)


def profile_column(
    *,
    column_name: str,
    normalized_data_type: str,
    sample_values: list[Any],
    exact_stats: ColumnExactStats | None,
    include_top_values: bool,
) -> dict:
    """Returns a dict of column_profiles field values for exactly one
    column. exact_stats is None whenever the exact-stats push-down for this
    specific column was unavailable this run (e.g. ExactStatsTimeoutError
    upstream) — the caller decides that per column, this function only
    reflects it in pattern_summary["exact_stats"]."""
    sample_size = len(sample_values)
    non_null_values = [v for v in sample_values if v is not None]

    if exact_stats is not None:
        null_count = exact_stats.null_count
        distinct_count = exact_stats.distinct_count
        total_row_count = exact_stats.total_row_count
        non_null_count = total_row_count - null_count
        exact_flag = True
    else:
        null_count = sample_size - len(non_null_values)
        distinct_count = len({_hashable(v) for v in non_null_values})
        total_row_count = sample_size
        non_null_count = len(non_null_values)
        exact_flag = False

    null_percentage = _round_decimal(100.0 * null_count / total_row_count, 2) if total_row_count else None
    distinct_percentage = _round_decimal(100.0 * distinct_count / total_row_count, 2) if total_row_count else None
    duplicate_count = max(0, non_null_count - distinct_count)
    duplicate_percentage = _round_decimal(100.0 * duplicate_count / total_row_count, 2) if total_row_count else None

    blank_count = sum(1 for v in non_null_values if isinstance(v, str) and v.strip() == "")
    blank_percentage = _round_decimal(100.0 * blank_count / sample_size, 2) if sample_size else _round_decimal(0.0, 2)

    result: dict[str, Any] = {
        "null_count": null_count,
        "null_percentage": null_percentage,
        "distinct_count": distinct_count,
        "distinct_percentage": distinct_percentage,
        "duplicate_count": duplicate_count,
        "duplicate_percentage": duplicate_percentage,
        "min_value": None,
        "max_value": None,
        "mean_value": None,
        "median_value": None,
        "mode_value": None,
        "min_length": None,
        "max_length": None,
        "avg_length": None,
        "outlier_count": None,
        "stddev_value": None,
        "sum_value": None,
        "value_distribution": None,
    }

    numeric_values = [v for v in non_null_values if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)]
    if numeric_values and normalized_data_type in ("INTEGER", "DECIMAL"):
        floats = [float(v) for v in numeric_values]
        result["mean_value"] = _round_decimal(statistics.fmean(floats))
        result["median_value"] = _round_decimal(statistics.median(floats))
        try:
            result["mode_value"] = str(statistics.mode(floats))
        except statistics.StatisticsError:
            result["mode_value"] = None
        result["stddev_value"] = _round_decimal(statistics.pstdev(floats)) if len(floats) > 1 else _round_decimal(0.0)
        result["sum_value"] = _round_decimal(math.fsum(floats))
        result["min_value"] = str(min(floats))
        result["max_value"] = str(max(floats))
        result["outlier_count"] = _iqr_outlier_count(floats)

    string_values = [v for v in non_null_values if isinstance(v, str)]
    if string_values and normalized_data_type in ("STRING", "TEXT"):
        lengths = [len(v) for v in string_values]
        result["min_length"] = min(lengths)
        result["max_length"] = max(lengths)
        result["avg_length"] = _round_decimal(statistics.fmean(lengths), 2)
        result["min_value"] = min(string_values)
        result["max_value"] = max(string_values)
        try:
            result["mode_value"] = statistics.mode(string_values)
        except statistics.StatisticsError:
            result["mode_value"] = None

    date_values = [v for v in non_null_values if isinstance(v, (date, datetime))]
    if date_values and normalized_data_type in ("DATE", "DATETIME"):
        result["min_value"] = str(min(date_values))
        result["max_value"] = str(max(date_values))

    if include_top_values and non_null_values:
        counts: dict[str, int] = {}
        for v in non_null_values:
            key = _truncate(str(v))
            counts[key] = counts.get(key, 0) + 1
        top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:MAX_TOP_VALUES]
        result["value_distribution"] = [{"value": k, "count": c} for k, c in top]

    result["pattern_summary"] = {
        "exact_stats": exact_flag,
        "blank_count": blank_count,
        "blank_percentage": float(blank_percentage),
    }
    return result


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return str(value)
    return value


def compute_dataset_duplicate_percentage(rows: list[dict[str, Any]]) -> Decimal | None:
    """Full-row duplication rate over the entire pulled sample — the ONLY
    dataset-level statistic this engine computes."""
    if not rows:
        return None
    safe_rows = [{k: _hashable(v) for k, v in row.items()} for row in rows]
    df = pd.DataFrame(safe_rows)
    duplicate_count = int(df.duplicated().sum())
    return _round_decimal(100.0 * duplicate_count / len(df), 2)

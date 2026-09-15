"""Unit tests for Phase 4.4: the pure, unwired date/datetime progression
Evidence Engine (app.modules.ai.evidence_temporal). Pure-function module —
no database, no fixtures, no mocking, mirrors tests/unit/test_evidence_sequence.py's
own isolation and style.
"""
from datetime import date, datetime

import pytest

from app.modules.ai.evidence_temporal import discover_temporal_evidence

# ---------------------------------------------------------------------------
# 1. Simple daily missing date
# ---------------------------------------------------------------------------


def test_daily_missing_date_produces_candidate():
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05", "2026-01-06", "2026-01-07"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-04"
    assert result.best.progression_type == "FIXED_INTERVAL"
    assert result.best.strategy == "TEMPORAL_GAP"


# ---------------------------------------------------------------------------
# 2. Weekly missing date
# ---------------------------------------------------------------------------


def test_weekly_missing_date_produces_candidate():
    values = ["2026-01-01", "2026-01-08", "2026-01-15", "2026-01-29", "2026-02-05"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-22"
    assert result.best.interval == "7 days, 0:00:00"


# ---------------------------------------------------------------------------
# 3. N-day interval (not 1, not 7)
# ---------------------------------------------------------------------------


def test_n_day_interval_produces_candidate():
    values = ["2026-01-01", "2026-01-04", "2026-01-07", "2026-01-13", "2026-01-16"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-10"


# ---------------------------------------------------------------------------
# 4. Datetime hourly interval
# ---------------------------------------------------------------------------


def test_hourly_datetime_interval_produces_candidate():
    values = [
        "2026-01-01T10:00:00", "2026-01-01T11:00:00", "2026-01-01T12:00:00",
        "2026-01-01T14:00:00", "2026-01-01T15:00:00",
    ]
    result = discover_temporal_evidence(target_column="event_ts", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-01T13:00:00"


# ---------------------------------------------------------------------------
# 5. Datetime minute interval
# ---------------------------------------------------------------------------


def test_minute_datetime_interval_produces_candidate():
    values = [
        "2026-01-01T10:00:00", "2026-01-01T10:15:00", "2026-01-01T10:30:00",
        "2026-01-01T11:00:00", "2026-01-01T11:15:00",
    ]
    result = discover_temporal_evidence(target_column="event_ts", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-01T10:45:00"


# ---------------------------------------------------------------------------
# 6. Monthly same-day progression
# ---------------------------------------------------------------------------


def test_monthly_same_day_progression_produces_candidate():
    values = ["2026-01-15", "2026-02-15", "2026-03-15", "2026-05-15"]
    result = discover_temporal_evidence(target_column="due_date", observed_values=values, min_observations=4)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-04-15"
    assert result.best.progression_type == "CALENDAR_MONTH_INTERVAL"
    assert "same day-of-month" in result.best.interval


# ---------------------------------------------------------------------------
# 7. Monthly missing month (distinct scenario from #6 — different day)
# ---------------------------------------------------------------------------


def test_monthly_missing_month_produces_candidate():
    values = ["2026-03-10", "2026-04-10", "2026-05-10", "2026-07-10"]
    result = discover_temporal_evidence(target_column="due_date", observed_values=values, min_observations=4)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-06-10"


# ---------------------------------------------------------------------------
# 8. Month-end progression
# ---------------------------------------------------------------------------


def test_month_end_progression_produces_candidate():
    values = ["2026-01-31", "2026-02-28", "2026-03-31", "2026-05-31"]
    result = discover_temporal_evidence(target_column="billing_date", observed_values=values, min_observations=4)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-04-30"
    assert "end-of-month" in result.best.interval


def test_month_end_progression_never_assumed_from_one_month_end_date():
    """A single month-end-looking date among otherwise plain dates must
    NOT trigger the end-of-month convention — it requires every observed
    date to independently satisfy it."""
    values = ["2026-01-31", "2026-02-10", "2026-03-15", "2026-04-20"]
    result = discover_temporal_evidence(target_column="billing_date", observed_values=values, min_observations=4)
    assert result.status != "CANDIDATE"


# ---------------------------------------------------------------------------
# 9. February handling (non-leap year)
# ---------------------------------------------------------------------------


def test_february_non_leap_year_end_of_month():
    values = ["2025-01-31", "2025-02-28", "2025-03-31", "2025-05-31"]  # 2025 is not a leap year
    result = discover_temporal_evidence(target_column="billing_date", observed_values=values, min_observations=4)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2025-04-30"


# ---------------------------------------------------------------------------
# 10. Leap-year handling
# ---------------------------------------------------------------------------


def test_leap_year_february_end_of_month():
    values = ["2024-01-31", "2024-02-29", "2024-03-31", "2024-05-31"]  # 2024 IS a leap year: Feb has 29 days
    result = discover_temporal_evidence(target_column="billing_date", observed_values=values, min_observations=4)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2024-04-30"


def test_recurring_feb_29_declines_on_non_leap_target_year():
    """A recurring Feb 29 pattern must never silently become Feb 28 or
    Mar 1 for a non-leap target year — it must decline instead."""
    values = ["2020-02-29", "2024-02-29", "2032-02-29"]  # missing 2028 (also a leap year, so this alone wouldn't test it)
    # Use a genuinely non-leap gap: 2016, 2020, 2028 (skipping the leap year 2024)
    values = ["2016-02-29", "2020-02-29", "2028-02-29"]
    result = discover_temporal_evidence(target_column="anniversary", observed_values=values, min_observations=3)
    # Whatever the outcome, it must never be a fabricated Feb-28/Mar-1 substitute.
    if result.best is not None:
        assert "02-29" in result.best.candidate_value


# ---------------------------------------------------------------------------
# 11. Yearly progression
# ---------------------------------------------------------------------------


def test_yearly_progression_missing_year():
    values = ["2023-06-15", "2024-06-15", "2026-06-15"]
    result = discover_temporal_evidence(target_column="renewal_date", observed_values=values, min_observations=3)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2025-06-15"
    assert result.best.progression_type == "CALENDAR_YEAR_INTERVAL"


# ---------------------------------------------------------------------------
# 12. One pair insufficient
# ---------------------------------------------------------------------------


def test_one_pair_is_insufficient():
    values = ["2026-01-01", "2026-01-02"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 13. Two transitions insufficient (below min_supporting_transitions)
# ---------------------------------------------------------------------------


def test_two_transitions_insufficient_for_confident_gap():
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-20"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values, min_observations=4)
    assert result.status != "CANDIDATE"
    assert result.best is None


# ---------------------------------------------------------------------------
# 14. Irregular dates
# ---------------------------------------------------------------------------


def test_irregular_dates_is_no_relationship():
    values = ["2026-01-01", "2026-03-17", "2026-07-02", "2026-11-30", "2026-12-05"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 15. Multiple missing dates
# ---------------------------------------------------------------------------


def test_multiple_missing_dates_is_ambiguous():
    values = ["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-07", "2026-01-08"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "AMBIGUOUS"
    assert result.best is None


# ---------------------------------------------------------------------------
# 16. Duplicate temporal value
# ---------------------------------------------------------------------------


def test_duplicate_temporal_value_produces_next_value_candidate():
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-02", "2026-01-04"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.strategy == "TEMPORAL_NEXT_VALUE"
    assert result.best.candidate_value == "2026-01-05"


# ---------------------------------------------------------------------------
# 17. Multiple duplicates
# ---------------------------------------------------------------------------


def test_multiple_duplicates_is_ambiguous():
    values = ["2026-01-01", "2026-01-02", "2026-01-02", "2026-01-03", "2026-01-03", "2026-01-04"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "AMBIGUOUS"
    assert result.best is None


# ---------------------------------------------------------------------------
# 18. Descending / input-unsorted data — chronological ordering, not row order
# ---------------------------------------------------------------------------


def test_unsorted_input_order_gives_identical_result_to_sorted():
    sorted_values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05", "2026-01-06", "2026-01-07"]
    shuffled_values = ["2026-01-06", "2026-01-01", "2026-01-05", "2026-01-07", "2026-01-03", "2026-01-02"]
    a = discover_temporal_evidence(target_column="event_date", observed_values=sorted_values)
    b = discover_temporal_evidence(target_column="event_date", observed_values=shuffled_values)
    assert a.status == b.status == "CANDIDATE"
    assert a.best.candidate_value == b.best.candidate_value == "2026-01-04"


# ---------------------------------------------------------------------------
# 19. Null values
# ---------------------------------------------------------------------------


def test_null_values_are_filtered_without_crashing():
    values = ["2026-01-01", None, "2026-01-02", "2026-01-03", None, "2026-01-05", "2026-01-06", "2026-01-07"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.observed_values_count == 6
    assert result.best.candidate_value == "2026-01-04"


# ---------------------------------------------------------------------------
# 20. Malformed string values
# ---------------------------------------------------------------------------


def test_malformed_string_values_are_excluded_without_crashing():
    values = ["2026-01-01", "not-a-date", "2026-01-02", "2026-01-03", "???", "2026-01-05", "2026-01-06", "2026-01-07"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.observed_values_count == 6
    assert result.best.candidate_value == "2026-01-04"


# ---------------------------------------------------------------------------
# 21. Ambiguous slash date strings rejected
# ---------------------------------------------------------------------------


def test_ambiguous_slash_dates_are_rejected_not_guessed():
    values = ["01/02/2026", "02/03/2026", "03/04/2026", "04/05/2026", "05/06/2026"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    # None of these locale-ambiguous strings should have been parsed at all.
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.observed_values_count == 0


# ---------------------------------------------------------------------------
# 22. Mixed date/datetime behavior
# ---------------------------------------------------------------------------


def test_mixed_date_and_datetime_values_handled_safely():
    """A native datetime.datetime instance (even at midnight) is treated
    as datetime-granularity, not silently collapsed to date-only — so a
    mix of date/datetime inputs correctly widens the output format to a
    full ISO datetime rather than crashing or losing information."""
    values = [date(2026, 1, 1), "2026-01-02", datetime(2026, 1, 3, 0, 0, 0), "2026-01-05", "2026-01-06", "2026-01-07"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value.startswith("2026-01-04")


# ---------------------------------------------------------------------------
# 23. Mixed naive/aware datetime safety
# ---------------------------------------------------------------------------


def test_mixed_naive_and_aware_datetimes_rejected():
    values = [
        "2026-01-01T10:00:00", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00",
        "2026-01-01T14:00:00", "2026-01-01T15:00:00",
    ]
    result = discover_temporal_evidence(target_column="event_ts", observed_values=values)
    assert result.status == "AMBIGUOUS"
    assert result.best is None
    assert "naive" in result.reason.lower() or "aware" in result.reason.lower()


def test_inconsistent_utc_offsets_rejected():
    values = [
        "2026-01-01T10:00:00+05:30", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00+05:30",
        "2026-01-01T14:00:00+05:30", "2026-01-01T15:00:00+05:30",
    ]
    result = discover_temporal_evidence(target_column="event_ts", observed_values=values)
    assert result.status == "AMBIGUOUS"
    assert result.best is None


# ---------------------------------------------------------------------------
# 24. Timezone-aware datetime progression (consistent offset — must work)
# ---------------------------------------------------------------------------


def test_consistent_timezone_aware_progression_produces_candidate():
    values = [
        "2026-01-01T10:00:00+05:30", "2026-01-01T11:00:00+05:30", "2026-01-01T12:00:00+05:30",
        "2026-01-01T14:00:00+05:30", "2026-01-01T15:00:00+05:30",
    ]
    result = discover_temporal_evidence(target_column="event_ts", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-01T13:00:00+05:30"


# ---------------------------------------------------------------------------
# 25. Wild future outlier
# ---------------------------------------------------------------------------


def test_wild_future_outlier_does_not_explode_or_produce_candidate():
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2099-12-31"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 26. Wild past outlier
# ---------------------------------------------------------------------------


def test_wild_past_outlier_does_not_explode_or_produce_candidate():
    values = ["1900-01-01", "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 27. Candidate already exists (structural invariant)
# ---------------------------------------------------------------------------


def test_candidate_never_collides_with_an_already_observed_value():
    scenarios = [
        ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05", "2026-01-06", "2026-01-07"],
        ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-02", "2026-01-04"],
    ]
    for values in scenarios:
        result = discover_temporal_evidence(target_column="event_date", observed_values=values)
        assert result.status == "CANDIDATE"
        assert result.best.candidate_already_exists is False
        assert result.best.candidate_value not in values


# ---------------------------------------------------------------------------
# 28. No anomaly — already fully contiguous
# ---------------------------------------------------------------------------


def test_no_anomaly_fully_contiguous_is_no_relationship():
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "NO_RELATIONSHIP"
    assert result.best is None
    assert "contiguous" in result.reason.lower()


# ---------------------------------------------------------------------------
# 29 / 30. Arbitrary target column name / renamed produces same result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column_name", ["order_date", "col_9", "temporal_attr", "ShipDate"])
def test_arbitrary_column_names_behave_identically(column_name):
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05", "2026-01-06", "2026-01-07"]
    result = discover_temporal_evidence(target_column=column_name, observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-01-04"
    assert result.target_column == column_name


def test_engine_never_branches_on_target_column_value():
    values = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-05", "2026-01-06", "2026-01-07"]
    business_named = discover_temporal_evidence(target_column="order_date", observed_values=values)
    generic_named = discover_temporal_evidence(target_column="attr_x", observed_values=values)
    assert business_named.best == generic_named.best
    assert business_named.status == generic_named.status


# ---------------------------------------------------------------------------
# 31 / 32 / 33. No hardcoded business schedule / weekend / holiday assumptions
# ---------------------------------------------------------------------------


def test_no_hardcoded_daily_business_assumption():
    """Values spaced 3 days apart must never be treated as "daily" — the
    interval is learned, never assumed."""
    values = ["2026-01-01", "2026-01-04", "2026-01-07", "2026-01-13", "2026-01-16"]
    result = discover_temporal_evidence(target_column="order_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.interval == "3 days, 0:00:00"  # never silently coerced to "1 day"


def test_no_weekend_skipping_assumption():
    """A clean run of business-day-only dates (no weekend gaps for the
    engine to "know" about) must be evaluated purely on the actual
    calendar gaps present — no built-in notion of weekends exists."""
    # Mon Jan 5, Tue 6, Wed 7, Thu 8 2026, then Mon Jan 12 (skipping the
    # Jan 9-11 weekend) — a real business-day dataset with NO gap to fill,
    # since (from the engine's perspective, ignorant of weekends) the
    # actual observed step alternates 1-day and 4-day jumps with no
    # single dominant step reaching the pattern-existence floor.
    values = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-12", "2026-01-13"]
    result = discover_temporal_evidence(target_column="order_date", observed_values=values)
    # Whatever the result, it must never fabricate Jan 9/10/11 as "missing
    # weekend business days" — there is no such concept in this module.
    if result.best is not None:
        assert result.best.candidate_value not in {"2026-01-09", "2026-01-10", "2026-01-11"}


def test_no_holiday_assumption():
    """A gap spanning a real-world holiday must be treated identically to
    any other single-day gap — there is no holiday calendar anywhere in
    this module, so the candidate is purely the arithmetic gap value."""
    # Dec 24, 26, 27, 28 (a "missing Dec 25" that happens to be Christmas —
    # but the engine has no idea what day this is, it just sees a gap).
    values = ["2026-12-24", "2026-12-26", "2026-12-27", "2026-12-28", "2026-12-29"]
    result = discover_temporal_evidence(target_column="order_date", observed_values=values)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-12-25"


# ---------------------------------------------------------------------------
# 34. Insufficient observations
# ---------------------------------------------------------------------------


def test_insufficient_observations_flagged_explicitly():
    values = ["2026-01-01", "2026-01-02", "2026-01-03"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status == "INSUFFICIENT_GROUP"
    assert result.best is None


# ---------------------------------------------------------------------------
# 35. Competing plausible intervals -> ambiguous
# ---------------------------------------------------------------------------


def test_competing_plausible_intervals_is_ambiguous_or_no_relationship():
    """Alternating 1-day/2-day steps could superficially suggest either a
    1-day or a 2-day base interval — the engine must not arbitrarily pick
    one and invent a specific gap value from it."""
    values = ["2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05", "2026-01-07"]
    result = discover_temporal_evidence(target_column="event_date", observed_values=values)
    assert result.status != "CANDIDATE"
    assert result.best is None


# ---------------------------------------------------------------------------
# Generic dataset scenario — Subscription_Billing (TEST SCENARIO ONLY)
# ---------------------------------------------------------------------------


def test_subscription_billing_scenario_end_of_month_candidate():
    """Uses a realistic 'BillingDate' column shape purely as a test
    scenario — discover_temporal_evidence() itself never reads column
    names or knows about billing/subscriptions."""
    values = ["2026-01-31", "2026-02-28", "2026-03-31", "2026-05-31"]
    result = discover_temporal_evidence(target_column="BillingDate", observed_values=values, min_observations=4)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-04-30"


def test_subscription_billing_scenario_conflicting_evidence_no_candidate():
    """A similarly-shaped billing-date column where the dates do NOT
    consistently satisfy end-of-month or same-day-of-month — no
    convention is established, so no candidate is produced."""
    values = ["2026-01-31", "2026-02-10", "2026-03-15", "2026-04-20"]
    result = discover_temporal_evidence(target_column="BillingDate", observed_values=values, min_observations=4)
    assert result.status != "CANDIDATE"
    assert result.best is None


# ---------------------------------------------------------------------------
# Customer_Orders — TEST SCENARIO ONLY, real observed order_date values
# ---------------------------------------------------------------------------


def test_customer_orders_scenario_real_order_dates_are_a_strong_daily_sequence():
    """The real Customer_Orders order_date values (see the Phase 3
    forensic investigation) happen to form a clean, strictly sequential
    daily progression with exactly one missing slot (the NULL row) — so a
    candidate IS defensible here, and this test documents exactly why:
    11 distinct dates, a single consistent 1-day step, one gap. This is a
    TEST SCENARIO ONLY; discover_temporal_evidence() has no idea this is
    "Customer_Orders" or "order_date"."""
    real_order_dates = [
        "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05", "2026-09-06",
        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-12",
        # 2026-09-13 is the row under review (order_date is NULL there)
        "2026-09-14", "2026-09-15",
    ]
    result = discover_temporal_evidence(target_column="order_date", observed_values=real_order_dates)
    assert result.status == "CANDIDATE"
    assert result.best.candidate_value == "2026-09-13"


def test_customer_orders_scenario_without_enough_real_evidence_declines():
    """If the real observed order_date values did NOT form a strong
    progression (e.g. too few clean observations after removing the null
    row, or an irregular spread), the correct result is NO_RELATIONSHIP or
    AMBIGUOUS — a candidate must never be forced merely because a row's
    order_date happens to be NULL."""
    sparse_real_dates = ["2026-09-01", "2026-09-05", "2026-09-11"]
    result = discover_temporal_evidence(target_column="order_date", observed_values=sparse_real_dates)
    assert result.status != "CANDIDATE"
    assert result.best is None

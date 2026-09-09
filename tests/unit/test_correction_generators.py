import uuid
from decimal import Decimal

import pytest

from app.modules.review.generators import CORRECTION_GENERATOR_REGISTRY, GeneratorContext
from app.modules.review.generators.median_fill import MedianFillGenerator
from app.modules.review.generators.mode_fill import ModeFillGenerator
from app.modules.review.generators.range_clamp import RangeClampGenerator
from app.modules.review.generators.trim_whitespace import TrimWhitespaceGenerator


class _FakeColumn:
    def __init__(self, normalized_data_type):
        self.normalized_data_type = normalized_data_type


class _FakeColumnProfile:
    def __init__(self, mode_value=None, median_value=None, distinct_percentage=None, stddev_value=None):
        self.mode_value = mode_value
        self.median_value = median_value
        self.distinct_percentage = distinct_percentage
        self.stddev_value = stddev_value


class _FakeValidationFailure:
    def __init__(self, failed_value=None):
        self.failed_value = failed_value


class _FakeRuleVersion:
    def __init__(self, definition=None):
        self.definition = definition or {}


def _ctx(*, column=None, column_profile=None, failed_value=None, definition=None):
    return GeneratorContext(
        issue=None, validation_failure=_FakeValidationFailure(failed_value=failed_value),
        column=column, column_profile=column_profile, rule_version=_FakeRuleVersion(definition=definition),
    )


# --- registry -------------------------------------------------------------

def test_registry_maps_exactly_the_approved_matrix() -> None:
    assert set(CORRECTION_GENERATOR_REGISTRY.keys()) == {"COMPLETENESS", "RANGE", "PATTERN"}
    assert {g.fix_type for g in CORRECTION_GENERATOR_REGISTRY["COMPLETENESS"]} == {"mode_fill", "median_fill"}
    assert {g.fix_type for g in CORRECTION_GENERATOR_REGISTRY["RANGE"]} == {"range_clamp"}
    assert {g.fix_type for g in CORRECTION_GENERATOR_REGISTRY["PATTERN"]} == {"trim_whitespace"}


@pytest.mark.parametrize("rule_type", ["UNIQUENESS", "DUPLICATE", "CROSS_COLUMN"])
def test_no_generator_registered_for_unmapped_rule_types(rule_type) -> None:
    assert rule_type not in CORRECTION_GENERATOR_REGISTRY


# --- mode_fill --------------------------------------------------------------

def test_mode_fill_applies_to_categorical_column_with_mode() -> None:
    ctx = _ctx(column=_FakeColumn("STRING"), column_profile=_FakeColumnProfile(mode_value="active", distinct_percentage=10))
    draft = ModeFillGenerator().generate(ctx)
    assert draft is not None
    assert draft.suggested_value == "active"
    assert Decimal("0.1") <= draft.confidence <= Decimal("0.9")


def test_mode_fill_returns_none_for_numeric_column() -> None:
    ctx = _ctx(column=_FakeColumn("INTEGER"), column_profile=_FakeColumnProfile(mode_value="5", distinct_percentage=10))
    assert ModeFillGenerator().generate(ctx) is None


def test_mode_fill_returns_none_without_column_profile() -> None:
    ctx = _ctx(column=_FakeColumn("STRING"), column_profile=None)
    assert ModeFillGenerator().generate(ctx) is None


def test_mode_fill_returns_none_when_mode_value_missing() -> None:
    ctx = _ctx(column=_FakeColumn("STRING"), column_profile=_FakeColumnProfile(mode_value=None, distinct_percentage=10))
    assert ModeFillGenerator().generate(ctx) is None


def test_mode_fill_confidence_formula_documented_value() -> None:
    # distinct_percentage=20 -> raw = 1 - 0.2 = 0.8 -> within [0.1,0.9] unclamped
    ctx = _ctx(column=_FakeColumn("STRING"), column_profile=_FakeColumnProfile(mode_value="x", distinct_percentage=20))
    draft = ModeFillGenerator().generate(ctx)
    assert draft.confidence == Decimal("0.8")


# --- median_fill --------------------------------------------------------------

def test_median_fill_applies_to_numeric_column_with_median_and_stddev() -> None:
    ctx = _ctx(column=_FakeColumn("DECIMAL"), column_profile=_FakeColumnProfile(median_value=Decimal("50"), stddev_value=Decimal("5")))
    draft = MedianFillGenerator().generate(ctx)
    assert draft is not None
    assert draft.suggested_value == "50"


def test_median_fill_returns_none_for_categorical_column() -> None:
    ctx = _ctx(column=_FakeColumn("STRING"), column_profile=_FakeColumnProfile(median_value=Decimal("50"), stddev_value=Decimal("5")))
    assert MedianFillGenerator().generate(ctx) is None


def test_median_fill_returns_none_without_stddev() -> None:
    ctx = _ctx(column=_FakeColumn("INTEGER"), column_profile=_FakeColumnProfile(median_value=Decimal("50"), stddev_value=None))
    assert MedianFillGenerator().generate(ctx) is None


def test_median_fill_zero_median_falls_back_to_fixed_midpoint() -> None:
    ctx = _ctx(column=_FakeColumn("INTEGER"), column_profile=_FakeColumnProfile(median_value=Decimal("0"), stddev_value=Decimal("2")))
    draft = MedianFillGenerator().generate(ctx)
    assert draft.confidence == Decimal("0.5")


def test_mode_fill_and_median_fill_never_both_apply_to_the_same_column() -> None:
    numeric_ctx = _ctx(column=_FakeColumn("INTEGER"), column_profile=_FakeColumnProfile(mode_value="5", median_value=Decimal("5"), distinct_percentage=10, stddev_value=Decimal("1")))
    categorical_ctx = _ctx(column=_FakeColumn("TEXT"), column_profile=_FakeColumnProfile(mode_value="5", median_value=Decimal("5"), distinct_percentage=10, stddev_value=Decimal("1")))

    assert ModeFillGenerator().generate(numeric_ctx) is None
    assert MedianFillGenerator().generate(numeric_ctx) is not None

    assert ModeFillGenerator().generate(categorical_ctx) is not None
    assert MedianFillGenerator().generate(categorical_ctx) is None


# --- range_clamp --------------------------------------------------------------

def test_range_clamp_clamps_below_min() -> None:
    ctx = _ctx(failed_value="-5", definition={"min": 0, "max": 100})
    draft = RangeClampGenerator().generate(ctx)
    assert draft is not None
    assert draft.suggested_value == "0"


def test_range_clamp_clamps_above_max() -> None:
    ctx = _ctx(failed_value="150", definition={"min": 0, "max": 100})
    draft = RangeClampGenerator().generate(ctx)
    assert draft.suggested_value == "100"


def test_range_clamp_returns_none_for_non_numeric_value() -> None:
    ctx = _ctx(failed_value="not-a-number", definition={"min": 0, "max": 100})
    assert RangeClampGenerator().generate(ctx) is None


def test_range_clamp_returns_none_when_value_within_bounds() -> None:
    ctx = _ctx(failed_value="50", definition={"min": 0, "max": 100})
    assert RangeClampGenerator().generate(ctx) is None


def test_range_clamp_fixed_confidence() -> None:
    ctx = _ctx(failed_value="-5", definition={"min": 0, "max": 100})
    draft = RangeClampGenerator().generate(ctx)
    assert draft.confidence == Decimal("0.85")


# --- trim_whitespace --------------------------------------------------------------

def test_trim_whitespace_fires_when_trim_fixes_the_match() -> None:
    ctx = _ctx(failed_value="  AB-123  ", definition={"regex": r"^[A-Z]{2}-\d{3}$"})
    draft = TrimWhitespaceGenerator().generate(ctx)
    assert draft is not None
    assert draft.suggested_value == "AB-123"
    assert draft.confidence == Decimal("0.95")


def test_trim_whitespace_returns_none_when_no_whitespace_present() -> None:
    ctx = _ctx(failed_value="bad-value", definition={"regex": r"^[A-Z]{2}-\d{3}$"})
    assert TrimWhitespaceGenerator().generate(ctx) is None


def test_trim_whitespace_returns_none_when_trimming_does_not_fix_it() -> None:
    ctx = _ctx(failed_value="  still-bad  ", definition={"regex": r"^[A-Z]{2}-\d{3}$"})
    assert TrimWhitespaceGenerator().generate(ctx) is None


def test_trim_whitespace_returns_none_without_regex_definition() -> None:
    ctx = _ctx(failed_value="  AB-123  ", definition={})
    assert TrimWhitespaceGenerator().generate(ctx) is None


def test_trim_whitespace_returns_none_for_null_failed_value() -> None:
    ctx = _ctx(failed_value=None, definition={"regex": r"^[A-Z]{2}-\d{3}$"})
    assert TrimWhitespaceGenerator().generate(ctx) is None

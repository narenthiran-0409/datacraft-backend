"""Unit tests for RuleDetectionService's pattern-matching heuristic
(_infer_category / _rule_definition_for_category) — pure functions, no DB,
no LLM. Uses lightweight fakes with just the attributes the heuristic
actually reads, rather than real ORM instances, since nothing here touches
a session."""
from types import SimpleNamespace

from app.modules.rules.detection_service import (
    CONFIDENCE_THRESHOLD,
    _infer_category,
    _rule_definition_for_category,
)


def _column(name: str, normalized_data_type: str | None) -> SimpleNamespace:
    return SimpleNamespace(name=name, normalized_data_type=normalized_data_type)


def _profile(
    *, distinct_percentage=None, value_distribution=None, min_value=None, max_value=None, mode_value=None
) -> SimpleNamespace:
    return SimpleNamespace(
        distinct_percentage=distinct_percentage, value_distribution=value_distribution,
        min_value=min_value, max_value=max_value, mode_value=mode_value,
    )


def test_email_detected_by_column_name() -> None:
    detection = _infer_category(_column("customer_email", "STRING"), None)
    assert detection.category == "email"
    assert detection.confidence >= CONFIDENCE_THRESHOLD

    rule_type, definition = _rule_definition_for_category(detection.category, None)
    assert rule_type == "PATTERN"
    assert "regex" in definition


def test_email_detected_by_value_pattern_when_name_gives_no_hint() -> None:
    profile = _profile(
        value_distribution=[
            {"value": "a@example.com", "count": 40},
            {"value": "b@example.com", "count": 40},
            {"value": "not-an-email", "count": 5},
        ]
    )
    detection = _infer_category(_column("contact_field", "STRING"), profile)
    assert detection.category == "email"
    assert detection.confidence >= CONFIDENCE_THRESHOLD


def test_phone_detected_by_column_name() -> None:
    detection = _infer_category(_column("mobile_number", "STRING"), None)
    assert detection.category == "phone"
    rule_type, definition = _rule_definition_for_category(detection.category, None)
    assert rule_type == "PATTERN"
    assert "regex" in definition


def test_date_detected_by_discovered_type_maps_to_completeness_not_pattern() -> None:
    """Deliberate deviation from the reference mapping: DATE/DATETIME
    columns are already typed at discovery, so format is already
    guaranteed — COMPLETENESS (never blank) is the honestly-recommendable
    check here, not a fragile PATTERN format regex."""
    detection = _infer_category(_column("created_at", "DATETIME"), None)
    assert detection.category == "date"
    assert detection.confidence >= CONFIDENCE_THRESHOLD

    rule_type, definition = _rule_definition_for_category(detection.category, None)
    assert rule_type == "COMPLETENESS"
    assert definition == {"max_null_percentage": 0}


def test_id_detected_by_name_and_high_uniqueness() -> None:
    profile = _profile(distinct_percentage=99.0)
    detection = _infer_category(_column("customer_id", "INTEGER"), profile)
    assert detection.category == "id"
    assert detection.confidence >= CONFIDENCE_THRESHOLD

    rule_type, definition = _rule_definition_for_category(detection.category, profile)
    assert rule_type == "UNIQUENESS"
    assert definition == {"max_duplicate_percentage": 0}


def test_id_detected_by_uniqueness_alone_without_name_hint() -> None:
    profile = _profile(distinct_percentage=98.0)
    detection = _infer_category(_column("reference_field", "STRING"), profile)
    assert detection.category == "id"
    assert detection.confidence >= CONFIDENCE_THRESHOLD


def test_numeric_detected_by_name_and_type() -> None:
    profile = _profile(min_value="0", max_value="9999.50")
    detection = _infer_category(_column("order_amount", "DECIMAL"), profile)
    assert detection.category == "numeric"
    assert detection.confidence >= CONFIDENCE_THRESHOLD

    rule_type, definition = _rule_definition_for_category(detection.category, profile)
    assert rule_type == "RANGE"
    assert definition == {"min": 0.0, "max": 9999.50}


def test_numeric_without_usable_stats_has_no_confident_local_rule() -> None:
    """Type alone is enough to categorize as numeric, but RANGE needs real
    min/max to be a meaningful recommendation — without it, this column
    must fall through to the AI fallback rather than getting a fabricated
    range."""
    detection = _infer_category(_column("balance", "DECIMAL"), None)
    assert detection.category == "numeric"
    assert detection.confidence >= CONFIDENCE_THRESHOLD

    assert _rule_definition_for_category(detection.category, None) is None


def test_id_name_token_matches_only_as_a_whole_word_not_a_substring() -> None:
    """Regression test: a naive `"no" in lowered_name` check would
    false-positive on "notes" (contains "no"). "acct_no" is the real
    positive case this token is meant to catch — separated by "_", a real
    word boundary; "notes" has no such boundary."""
    detection = _infer_category(_column("acct_no", "STRING"), None)
    assert detection.category == "id"

    detection = _infer_category(_column("notes", "TEXT"), None)
    assert detection.category != "id"


def test_free_text_fallback_is_below_confidence_threshold() -> None:
    detection = _infer_category(_column("notes", "TEXT"), None)
    assert detection.category == "free_text"
    assert detection.confidence < CONFIDENCE_THRESHOLD
    assert _rule_definition_for_category(detection.category, None) is None


def test_boolean_typed_column_falls_through_to_ai_fallback() -> None:
    """No single-column rule type honestly fits a boolean well — this
    should route to the AI fallback rather than force COMPLETENESS or
    anything else onto every boolean column indiscriminately."""
    detection = _infer_category(_column("is_active", "BOOLEAN"), None)
    assert detection.confidence < CONFIDENCE_THRESHOLD

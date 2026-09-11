"""Unit tests for AISuggestionService's defensive parsing of the
ai_rule_recommendation LLM response (_parse_rule_recommendations) — pure
function, no DB, no LLM call."""
import pytest

from app.core.exceptions import AIResponseInvalidError
from app.modules.ai.suggestion_service import _parse_rule_recommendations


def test_parses_a_clean_json_array() -> None:
    text = (
        '[{"column_name": "email", "rule_type": "PATTERN", '
        '"definition": {"regex": "^.+@.+$"}, "confidence": 0.9, "reasoning": "looks like email"}]'
    )
    result = _parse_rule_recommendations(text)
    assert len(result) == 1
    assert result[0]["column_name"] == "email"
    assert result[0]["rule_type"] == "PATTERN"


def test_strips_a_markdown_json_code_fence() -> None:
    text = '```json\n[{"column_name": "age", "rule_type": "RANGE", "definition": {"min": 0, "max": 120}}]\n```'
    result = _parse_rule_recommendations(text)
    assert len(result) == 1
    assert result[0]["column_name"] == "age"


def test_strips_a_bare_code_fence_without_the_json_label() -> None:
    text = '```\n[{"column_name": "x", "rule_type": "COMPLETENESS", "definition": {}}]\n```'
    result = _parse_rule_recommendations(text)
    assert len(result) == 1


def test_empty_array_is_a_valid_answer_not_an_error() -> None:
    assert _parse_rule_recommendations("[]") == []


def test_non_dict_array_entries_are_dropped_not_errored() -> None:
    text = '[{"column_name": "a", "rule_type": "COMPLETENESS", "definition": {}}, "garbage", 42]'
    result = _parse_rule_recommendations(text)
    assert len(result) == 1
    assert result[0]["column_name"] == "a"


def test_invalid_json_raises_ai_response_invalid_error() -> None:
    with pytest.raises(AIResponseInvalidError):
        _parse_rule_recommendations("this is not json at all")


def test_a_json_object_instead_of_an_array_raises_ai_response_invalid_error() -> None:
    """The model must return an array (one entry per recommended column) —
    a single object, even a well-formed one, isn't the agreed shape and
    can't be safely iterated as recommendations."""
    with pytest.raises(AIResponseInvalidError):
        _parse_rule_recommendations('{"column_name": "a", "rule_type": "COMPLETENESS"}')

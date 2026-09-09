"""Unit tests for AnthropicProvider against a mocked SDK client — zero
real external LLM calls anywhere in this file."""
from unittest.mock import MagicMock

import anthropic
import pytest

from app.core.exceptions import AIProviderUnavailableError, AIResponseInvalidError
from app.modules.ai.providers import ProviderMessage
from app.modules.ai.providers.anthropic_provider import AnthropicProvider


def _make_provider(mock_client) -> AnthropicProvider:
    provider = AnthropicProvider(api_key="fake-key", max_retry_attempts=2)
    provider._client = mock_client
    return provider


def _fake_response(text: str = "hello", input_tokens: int = 10, output_tokens: int = 5, stop_reason: str = "end_turn"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    response = MagicMock()
    response.content = [block]
    response.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    response.stop_reason = stop_reason
    response.model = "claude-test"
    return response


def test_send_success_returns_normalized_response() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _fake_response(text="the explanation")
    provider = _make_provider(mock_client)

    result = provider.send(
        system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
        max_tokens=100, timeout_seconds=30,
    )

    assert result.content == "the explanation"
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert result.latency_ms >= 0
    assert result.raw_metadata["stop_reason"] == "end_turn"


def test_send_timeout_retries_then_raises_provider_unavailable() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = anthropic.APITimeoutError(request=MagicMock())
    provider = _make_provider(mock_client)

    with pytest.raises(AIProviderUnavailableError):
        provider.send(
            system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
            max_tokens=100, timeout_seconds=30,
        )
    assert mock_client.messages.create.call_count == 2  # max_retry_attempts=2


def test_send_timeout_then_success_on_retry_succeeds() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        anthropic.APITimeoutError(request=MagicMock()),
        _fake_response(text="recovered"),
    ]
    provider = _make_provider(mock_client)

    result = provider.send(
        system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
        max_tokens=100, timeout_seconds=30,
    )
    assert result.content == "recovered"
    assert mock_client.messages.create.call_count == 2


def test_send_provider_error_not_retried_raises_immediately() -> None:
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())
    provider = _make_provider(mock_client)

    with pytest.raises(AIProviderUnavailableError):
        provider.send(
            system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
            max_tokens=100, timeout_seconds=30,
        )
    assert mock_client.messages.create.call_count == 1  # non-timeout errors are not retried


def test_send_malformed_response_no_content_blocks_raises_invalid() -> None:
    mock_client = MagicMock()
    response = MagicMock()
    response.content = []
    mock_client.messages.create.return_value = response
    provider = _make_provider(mock_client)

    with pytest.raises(AIResponseInvalidError):
        provider.send(
            system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
            max_tokens=100, timeout_seconds=30,
        )


def test_send_malformed_response_no_text_block_raises_invalid() -> None:
    mock_client = MagicMock()
    non_text_block = MagicMock()
    non_text_block.type = "tool_use"
    response = MagicMock()
    response.content = [non_text_block]
    mock_client.messages.create.return_value = response
    provider = _make_provider(mock_client)

    with pytest.raises(AIResponseInvalidError):
        provider.send(
            system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
            max_tokens=100, timeout_seconds=30,
        )


def test_send_missing_usage_returns_none_token_counts() -> None:
    mock_client = MagicMock()
    response = _fake_response()
    response.usage = None
    mock_client.messages.create.return_value = response
    provider = _make_provider(mock_client)

    result = provider.send(
        system="sys", messages=[ProviderMessage(role="user", content="hi")], model="claude-test",
        max_tokens=100, timeout_seconds=30,
    )
    assert result.input_tokens is None
    assert result.output_tokens is None

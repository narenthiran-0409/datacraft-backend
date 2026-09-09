import time

import anthropic

from app.core.exceptions import AIProviderUnavailableError, AIResponseInvalidError
from app.modules.ai.providers import AIProvider, ProviderMessage, ProviderResponse


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key: str, *, max_retry_attempts: int) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._max_retry_attempts = max_retry_attempts

    def send(
        self, *, system: str, messages: list[ProviderMessage], model: str, max_tokens: int, timeout_seconds: int,
    ) -> ProviderResponse:
        payload = [{"role": m.role, "content": m.content} for m in messages]

        last_error: Exception | None = None
        for attempt in range(1, self._max_retry_attempts + 1):
            start = time.monotonic()
            try:
                response = self._client.messages.create(
                    model=model, max_tokens=max_tokens, system=system, messages=payload, timeout=timeout_seconds,
                )
            except anthropic.APITimeoutError as exc:
                last_error = exc
                continue
            except anthropic.AnthropicError as exc:
                # Non-timeout provider/connection error — not retried, fails fast.
                raise AIProviderUnavailableError(f"Anthropic provider error: {exc}") from exc

            latency_ms = int((time.monotonic() - start) * 1000)
            return self._parse_response(response, latency_ms)

        raise AIProviderUnavailableError(
            f"Anthropic provider timed out after {self._max_retry_attempts} attempt(s)"
        ) from last_error

    @staticmethod
    def _parse_response(response, latency_ms: int) -> ProviderResponse:
        blocks = getattr(response, "content", None)
        if not blocks:
            raise AIResponseInvalidError("Anthropic response contained no content blocks")

        text_parts = [block.text for block in blocks if getattr(block, "type", None) == "text"]
        if not text_parts:
            raise AIResponseInvalidError("Anthropic response contained no text content block")

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage is not None else None
        output_tokens = getattr(usage, "output_tokens", None) if usage is not None else None

        return ProviderResponse(
            content="".join(text_parts),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            raw_metadata={"stop_reason": getattr(response, "stop_reason", None), "model": getattr(response, "model", None)},
        )

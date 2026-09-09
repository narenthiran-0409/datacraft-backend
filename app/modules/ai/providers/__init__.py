"""AIProvider ABC — the only contract AIOrchestratorService is allowed to
depend on. No route or business-logic module may call a concrete provider
or an LLM SDK directly; everything goes through AIOrchestratorService,
which goes through this ABC. Adding OpenAI/Gemini later means adding a new
AIProvider subclass here — the orchestration contract never changes."""
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderMessage:
    role: str  # "user" or "assistant" — provider-facing role, not ai_messages.role
    content: str


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    raw_metadata: dict


class AIProvider(ABC):
    name: str

    @abstractmethod
    def send(
        self, *, system: str, messages: list[ProviderMessage], model: str, max_tokens: int, timeout_seconds: int,
    ) -> ProviderResponse:
        """Sends a request to the provider and returns a normalized
        response. Implementations must raise AIProviderUnavailableError on
        timeout/connection/provider-side failure, and AIResponseInvalidError
        on a malformed/unexpected response shape — never let a raw SDK
        exception escape this boundary."""

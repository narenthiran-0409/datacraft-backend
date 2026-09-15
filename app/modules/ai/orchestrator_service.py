import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AIDisabledError, AIProviderUnavailableError
from app.db.models import AIPromptVersion, AIUsageLog, User
from app.modules.ai.context import compute_input_context_hash
from app.modules.ai.prompt_service import PromptVersionService
from app.modules.ai.providers import AIProvider, ProviderMessage
from app.modules.ai.providers.anthropic_provider import AnthropicProvider

_PROVIDER_REGISTRY: dict[str, type[AIProvider]] = {
    "anthropic": AnthropicProvider,
}


@dataclass(frozen=True)
class OrchestratorResult:
    text: str
    provider: str
    model: str
    prompt_version: AIPromptVersion
    context_hash: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    usage_log_id: uuid.UUID


class AIOrchestratorService:
    """The SOLE caller of any concrete AIProvider anywhere in the
    application — no route or other business-logic module may call
    AnthropicProvider or the Anthropic SDK directly. Resolves AI_ENABLED
    gating, provider/model selection, prompt version resolution, computes
    the deterministic input-context hash, calls the provider, and writes
    ai_usage_logs for every call it makes (success or failure alike, per
    Decision 12 — usage/cost tracking is unconditional)."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._prompt_service = PromptVersionService(db)

    def _get_provider(self) -> AIProvider:
        if not settings.AI_ENABLED:
            raise AIDisabledError("AI is disabled (AI_ENABLED=false)")

        provider_cls = _PROVIDER_REGISTRY.get(settings.AI_DEFAULT_PROVIDER)
        if provider_cls is None:
            raise AIProviderUnavailableError(f"No provider registered for '{settings.AI_DEFAULT_PROVIDER}'")

        if not settings.ANTHROPIC_API_KEY:
            raise AIProviderUnavailableError("ANTHROPIC_API_KEY is not configured")

        return provider_cls(settings.ANTHROPIC_API_KEY, max_retry_attempts=settings.AI_RETRY_MAX_ATTEMPTS)

    def run(
        self,
        *,
        prompt_key: str,
        context: dict,
        actor: User,
        conversation_id: uuid.UUID | None = None,
        ai_suggestion_id_for_usage_log: uuid.UUID | None = None,
        model: str | None = None,
        max_tokens: int = 1024,
    ) -> OrchestratorResult:
        """Raises AIDisabledError before any provider call if AI_ENABLED is
        false — no usage log is written in that case, since no call was
        ever attempted."""
        provider = self._get_provider()  # raises AIDisabledError first, before any other resolution
        prompt_version = self._prompt_service.resolve_active(prompt_key)
        resolved_model = model or prompt_version.default_model or settings.AI_DEFAULT_MODEL
        context_hash = compute_input_context_hash(context)

        system_prompt = prompt_version.template
        user_message = ProviderMessage(role="user", content=_render_context(context))

        start = time.monotonic()
        try:
            response = provider.send(
                system=system_prompt, messages=[user_message], model=resolved_model, max_tokens=max_tokens,
                timeout_seconds=settings.AI_REQUEST_TIMEOUT_SECONDS,
            )
        except AIProviderUnavailableError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            usage_log = self._log_usage(
                actor=actor, provider=provider.name, model=resolved_model, prompt_version_id=prompt_version.id,
                conversation_id=conversation_id, ai_suggestion_id=ai_suggestion_id_for_usage_log,
                input_tokens=None, output_tokens=None, latency_ms=latency_ms, cost_estimate=None,
            )
            # Phase 4.9: callers (e.g. AISuggestionService.generate_corrections)
            # isolate one issue's failure with their own try/except that
            # calls self._db.rollback() — which would otherwise silently
            # discard this just-written usage log too, since _log_usage only
            # flushes (see its own commit, added this phase, for why that's
            # no longer sufficient on its own). Attaching the id onto the
            # exception lets such a caller still backfill
            # ai_usage_logs.ai_suggestion_id once it creates its own
            # (possibly degraded/fallback) ai_suggestions row for this
            # attempt — never fabricating a link, only completing one for a
            # usage log that verifiably already exists.
            exc.usage_log_id = usage_log.id
            raise

        usage_log = self._log_usage(
            actor=actor, provider=provider.name, model=resolved_model, prompt_version_id=prompt_version.id,
            conversation_id=conversation_id, ai_suggestion_id=ai_suggestion_id_for_usage_log,
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            latency_ms=response.latency_ms, cost_estimate=None,  # provider doesn't return cost — never fabricated
        )

        return OrchestratorResult(
            text=response.content, provider=provider.name, model=resolved_model, prompt_version=prompt_version,
            context_hash=context_hash, input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            latency_ms=response.latency_ms, usage_log_id=usage_log.id,
        )

    def _log_usage(
        self, *, actor: User, provider: str, model: str, prompt_version_id: uuid.UUID,
        conversation_id: uuid.UUID | None, ai_suggestion_id: uuid.UUID | None,
        input_tokens: int | None, output_tokens: int | None, latency_ms: int, cost_estimate: Decimal | None,
    ) -> AIUsageLog:
        usage_log = AIUsageLog(
            conversation_id=conversation_id, ai_suggestion_id=ai_suggestion_id, user_id=actor.id,
            provider=provider, model=model, prompt_version_id=prompt_version_id,
            input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms,
            cost_estimate=cost_estimate,
        )
        self._db.add(usage_log)
        self._db.flush()
        # Phase 4.9: committed immediately, not merely flushed — mirrors the
        # already-established precedent in AISuggestionService._correction_context
        # ("Committed immediately so this audit trail survives even if the
        # subsequent LLM call fails and the per-issue handler rolls back").
        # Usage/cost tracking is supposed to be unconditional (see this
        # class's own docstring); without this commit, a per-issue
        # rollback triggered by a LATER step (response parsing, evidence
        # safety enforcement) would silently destroy the record of a real,
        # already-executed, already-billed provider call.
        self._db.commit()
        self._db.refresh(usage_log)
        return usage_log


def _render_context(context: dict) -> str:
    """Serializes the assembled metadata-only context into the user-turn
    message sent to the provider. Deliberately the same canonical JSON
    shape compute_input_context_hash hashes, so what was hashed is
    exactly what was sent."""
    return json.dumps(context, sort_keys=True, separators=(",", ":"), default=str)


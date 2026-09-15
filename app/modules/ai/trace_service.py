"""Phase 4.9 — AI usage/correction traceability.

Read-only. Never invokes AI, never touches candidate/evidence inference,
never mutates anything. Answers, deterministically, from data already
persisted by Phase 6 (correction_suggestions), Phase 12 (ai_suggestions,
ai_usage_logs, ai_prompt_versions), and this phase's own backfill (see
AISuggestionService.generate_corrections and AIOrchestratorService.run):

    CorrectionSuggestion -> ai_suggestion_id -> AISuggestion
        -> AIUsageLog(s) (matched by ai_suggestion_id)
        -> AIPromptVersion (from AISuggestion.prompt_version_id — the
           version that was ACTUALLY active at call time, never "whatever
           is active today")

A deterministic (RULE_BASED) correction suggestion has no ai_suggestion_id
at all — reported as is_llm_backed=False, usage=[], never a fabricated
or fake usage record.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import SuggestionNotFoundError
from app.db.models import AIPromptVersion, AISuggestion, AIUsageLog, CorrectionSuggestion

LINKAGE_OK = "OK"
LINKAGE_NO_AI_CALL = "NO_AI_CALL"
LINKAGE_BROKEN = "BROKEN_LINKAGE"


@dataclass(frozen=True)
class AITracePromptInfo:
    id: uuid.UUID
    key: str
    version_number: int


@dataclass(frozen=True)
class AITraceUsageEntry:
    id: uuid.UUID
    provider: str
    model: str
    prompt_version_id: uuid.UUID | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None
    status: str  # "SUCCESS" | "FAILED" — derived from token presence, never stored/fabricated
    created_at: datetime


@dataclass(frozen=True)
class AITraceResult:
    correction_suggestion_id: uuid.UUID
    ai_suggestion_id: uuid.UUID | None
    is_llm_backed: bool
    linkage_status: str  # OK | NO_AI_CALL | BROKEN_LINKAGE
    provider: str | None
    model: str | None
    prompt: AITracePromptInfo | None
    usage: list[AITraceUsageEntry]


class AITraceService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_trace(self, correction_suggestion_id: uuid.UUID) -> AITraceResult:
        suggestion = self._db.get(CorrectionSuggestion, correction_suggestion_id)
        if suggestion is None:
            raise SuggestionNotFoundError(f"Correction suggestion {correction_suggestion_id} not found")

        if suggestion.source != "AI" or suggestion.ai_suggestion_id is None:
            # Legitimately deterministic — never call this "broken".
            return AITraceResult(
                correction_suggestion_id=suggestion.id, ai_suggestion_id=None, is_llm_backed=False,
                linkage_status=LINKAGE_NO_AI_CALL, provider=None, model=None, prompt=None, usage=[],
            )

        ai_suggestion = self._db.get(AISuggestion, suggestion.ai_suggestion_id)
        if ai_suggestion is None:
            # The FK column names a row that no longer resolves — never
            # silently reinterpret this as "no AI call"; that would hide a
            # real data-integrity problem from the audit trail.
            return AITraceResult(
                correction_suggestion_id=suggestion.id, ai_suggestion_id=suggestion.ai_suggestion_id,
                is_llm_backed=True, linkage_status=LINKAGE_BROKEN, provider=None, model=None, prompt=None, usage=[],
            )

        prompt_version = self._db.get(AIPromptVersion, ai_suggestion.prompt_version_id)
        prompt_info = (
            AITracePromptInfo(
                id=prompt_version.id, key=prompt_version.prompt_key, version_number=prompt_version.version_number,
            )
            if prompt_version is not None
            else None
        )

        usage_logs = list(
            self._db.execute(
                select(AIUsageLog)
                .where(AIUsageLog.ai_suggestion_id == ai_suggestion.id)
                .order_by(AIUsageLog.created_at)
            ).scalars()
        )
        usage_entries = [
            AITraceUsageEntry(
                id=log.id, provider=log.provider, model=log.model, prompt_version_id=log.prompt_version_id,
                input_tokens=log.input_tokens, output_tokens=log.output_tokens,
                total_tokens=(
                    log.input_tokens + log.output_tokens
                    if log.input_tokens is not None and log.output_tokens is not None
                    else None
                ),
                latency_ms=log.latency_ms,
                status="SUCCESS" if log.input_tokens is not None else "FAILED",
                created_at=log.created_at,
            )
            for log in usage_logs
        ]

        return AITraceResult(
            correction_suggestion_id=suggestion.id, ai_suggestion_id=ai_suggestion.id, is_llm_backed=True,
            linkage_status=LINKAGE_OK, provider=ai_suggestion.provider, model=ai_suggestion.model,
            prompt=prompt_info, usage=usage_entries,
        )

"""Phase 4.9 — unit-adjacent tests for AITraceService (real Postgres for
model relationships, mirroring tests/unit/test_ai_orchestrator.py's own
stated convention). No real external LLM calls anywhere in this file."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    AIPromptVersion,
    AISuggestion,
    AIUsageLog,
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    Schema,
    User,
)
from app.modules.ai.trace_service import (
    LINKAGE_BROKEN,
    LINKAGE_NO_AI_CALL,
    LINKAGE_OK,
    AITraceService,
)


def _make_issue(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str) -> Issue:
    """Minimal real Issue anchor — one COMPLETENESS rule, one failing row —
    reused across scenarios so each test controls its own
    CorrectionSuggestion/AISuggestion/AIUsageLog rows directly."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.review.service import ReviewService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, NULL)"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"p49_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="MEDIUM",
        error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
    )

    validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name=f"p49_{uuid.uuid4().hex[:6]}", actor=admin_user
    )
    db.expire_all()
    return db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()


def _make_prompt_version(db: Session, admin_user: User, *, version_number: int = 4) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key="ai_correction", version_number=version_number, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _make_ai_suggestion(db: Session, admin_user: User, issue: Issue, prompt_version: AIPromptVersion, *, provider="anthropic", model="claude-test") -> AISuggestion:
    suggestion = AISuggestion(
        suggestion_type="CORRECTION", source_context_type="ISSUE", source_context_id=issue.id,
        content={"text": "..."}, provider=provider, model=model, prompt_version_id=prompt_version.id,
        conversation_id=None, requested_by=admin_user.id, status="PROPOSED",
        response_metadata={"input_context_hash": "abc123"},
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def _make_correction_suggestion(db: Session, issue: Issue, *, source: str, ai_suggestion_id=None, suggested_value="x") -> CorrectionSuggestion:
    cs = CorrectionSuggestion(
        issue_id=issue.id, source=source, ai_suggestion_id=ai_suggestion_id, suggested_value=suggested_value,
        confidence=Decimal("1.0"), category="AI_HIGH_CONFIDENCE" if source == "AI" else "DETERMINISTIC",
        fix_type="AI_PROPOSED" if source == "AI" else "RULE_PROPOSED", reasoning=None, is_selected=False,
    )
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return cs


def _make_usage_log(db: Session, admin_user: User, ai_suggestion_id, prompt_version_id, *, provider="anthropic", model="claude-test", input_tokens=100, output_tokens=50, latency_ms=250) -> AIUsageLog:
    log = AIUsageLog(
        conversation_id=None, ai_suggestion_id=ai_suggestion_id, user_id=admin_user.id, provider=provider,
        model=model, prompt_version_id=prompt_version_id, input_tokens=input_tokens, output_tokens=output_tokens,
        latency_ms=latency_ms, cost_estimate=None,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


def test_llm_backed_trace_links_suggestion_usage_and_prompt(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p49_ok_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        prompt_version = _make_prompt_version(db, admin_user)
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, prompt_version)
        usage_log = _make_usage_log(db, admin_user, ai_suggestion.id, prompt_version.id)
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id)

        trace = AITraceService(db).get_trace(cs.id)

        assert trace.correction_suggestion_id == cs.id
        assert trace.ai_suggestion_id == ai_suggestion.id
        assert trace.is_llm_backed is True
        assert trace.linkage_status == LINKAGE_OK
        assert trace.provider == "anthropic"
        assert trace.model == "claude-test"
        assert trace.prompt.key == "ai_correction"
        assert trace.prompt.version_number == 4
        assert len(trace.usage) == 1
        assert trace.usage[0].id == usage_log.id
        assert trace.usage[0].input_tokens == 100
        assert trace.usage[0].output_tokens == 50
        assert trace.usage[0].total_tokens == 150
        assert trace.usage[0].latency_ms == 250
        assert trace.usage[0].status == "SUCCESS"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_deterministic_only_correction_has_no_fake_llm_linkage(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p49_det_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        cs = _make_correction_suggestion(db, issue, source="RULE_BASED", ai_suggestion_id=None)

        trace = AITraceService(db).get_trace(cs.id)

        assert trace.is_llm_backed is False
        assert trace.ai_suggestion_id is None
        assert trace.linkage_status == LINKAGE_NO_AI_CALL
        assert trace.usage == []
        assert trace.prompt is None
        assert trace.provider is None
        assert trace.model is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_failed_ai_execution_represented_truthfully(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p49_fail_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        prompt_version = _make_prompt_version(db, admin_user)
        # Mirrors the real fallback path in AISuggestionService.generate_corrections:
        # ai_suggestions row with provider/model="unavailable", but the
        # usage log (if a real attempt was made) still carries the REAL
        # provider name and null token counts.
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, prompt_version, provider="unavailable", model="unavailable")
        usage_log = _make_usage_log(
            db, admin_user, ai_suggestion.id, prompt_version.id, provider="anthropic", model="claude-test",
            input_tokens=None, output_tokens=None, latency_ms=1500,
        )
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id, suggested_value="")

        trace = AITraceService(db).get_trace(cs.id)

        assert trace.is_llm_backed is True
        assert trace.linkage_status == LINKAGE_OK
        assert len(trace.usage) == 1
        assert trace.usage[0].status == "FAILED"
        assert trace.usage[0].input_tokens is None
        assert trace.usage[0].total_tokens is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_multiple_usage_logs_are_all_returned_deterministically(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p49_multi_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        prompt_version = _make_prompt_version(db, admin_user)
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, prompt_version)
        log1 = _make_usage_log(db, admin_user, ai_suggestion.id, prompt_version.id, input_tokens=None, output_tokens=None)
        log2 = _make_usage_log(db, admin_user, ai_suggestion.id, prompt_version.id, input_tokens=80, output_tokens=40)
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id)

        trace = AITraceService(db).get_trace(cs.id)

        assert len(trace.usage) == 2
        assert [u.id for u in trace.usage] == [log1.id, log2.id]  # ordered by created_at
        assert trace.usage[0].status == "FAILED"
        assert trace.usage[1].status == "SUCCESS"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_missing_correction_suggestion_raises_not_found(db):
    from app.core.exceptions import SuggestionNotFoundError

    with pytest.raises(SuggestionNotFoundError):
        AITraceService(db).get_trace(uuid.uuid4())


def test_broken_ai_linkage_is_reported_not_silently_treated_as_deterministic(db, redis_client, admin_user, pg_connection):
    """The real FK on correction_suggestions.ai_suggestion_id makes a
    genuinely dangling reference impossible via normal writes — proven
    here by simulating the defensive branch directly (the row the FK
    would have referenced is simply never resolved), confirming the
    service reports BROKEN_LINKAGE rather than silently downgrading to
    'no AI call' and hiding a real integrity problem."""
    table_name = f"dq_p49_broken_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        prompt_version = _make_prompt_version(db, admin_user)
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, prompt_version)
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id)

        real_get = db.get

        def _patched_get(model, ident, *args, **kwargs):
            if model is AISuggestion and ident == ai_suggestion.id:
                return None
            return real_get(model, ident, *args, **kwargs)

        with patch.object(db, "get", side_effect=_patched_get):
            trace = AITraceService(db).get_trace(cs.id)

        assert trace.linkage_status == LINKAGE_BROKEN
        assert trace.is_llm_backed is True
        assert trace.usage == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_historical_trace_independent_of_currently_active_prompt_version(db, redis_client, admin_user, pg_connection):
    """A prompt version can be superseded (is_active flipped False on the
    OLD row when a new one is seeded — see scripts/seed_ai_prompt_versions.py)
    after the AI call happened. The trace must still report the version
    that was ACTUALLY used, never 'whatever prompt is active today'."""
    table_name = f"dq_p49_hist_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        old_version = _make_prompt_version(db, admin_user, version_number=4)
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, old_version)
        _make_usage_log(db, admin_user, ai_suggestion.id, old_version.id)
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id)

        # A new prompt version is seeded/activated afterward — the OLD
        # version row itself is retired (is_active=False) but never
        # deleted/rewritten, exactly mirroring seed_ai_prompt_versions.py's
        # real retire-and-insert behavior.
        old_version.is_active = False
        db.commit()
        new_version = AIPromptVersion(
            prompt_key="ai_correction", version_number=5, template="new template", default_model="claude-test-2",
            is_active=True, created_by=admin_user.id,
        )
        db.add(new_version)
        db.commit()

        trace = AITraceService(db).get_trace(cs.id)

        assert trace.prompt.version_number == 4  # the version actually used, not the new active one (5)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_historical_trace_independent_of_current_env_provider_model(db, redis_client, admin_user, pg_connection, monkeypatch):
    """Changing settings.AI_DEFAULT_PROVIDER/AI_DEFAULT_MODEL after the
    fact must never change what a historical trace reports — it reads
    only what was persisted at call time (ai_suggestions.provider/model,
    ai_usage_logs.provider/model), never current settings."""
    from app.core.config import settings

    table_name = f"dq_p49_env_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        prompt_version = _make_prompt_version(db, admin_user)
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, prompt_version, provider="anthropic", model="model-y-that-actually-ran")
        _make_usage_log(db, admin_user, ai_suggestion.id, prompt_version.id, provider="anthropic", model="model-y-that-actually-ran")
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id)

        monkeypatch.setattr(settings, "AI_DEFAULT_PROVIDER", "some_other_provider")
        monkeypatch.setattr(settings, "AI_DEFAULT_MODEL", "model-x-configured-today")

        trace = AITraceService(db).get_trace(cs.id)

        assert trace.model == "model-y-that-actually-ran"
        assert trace.usage[0].model == "model-y-that-actually-ran"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_trace_never_exposes_secrets_or_raw_prompt_or_response(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p49_privacy_{uuid.uuid4().hex[:8]}"
    try:
        issue = _make_issue(db, redis_client, admin_user, pg_connection, table_name)
        prompt_version = _make_prompt_version(db, admin_user)
        ai_suggestion = _make_ai_suggestion(db, admin_user, issue, prompt_version)
        _make_usage_log(db, admin_user, ai_suggestion.id, prompt_version.id)
        cs = _make_correction_suggestion(db, issue, source="AI", ai_suggestion_id=ai_suggestion.id)

        trace = AITraceService(db).get_trace(cs.id)
        serialized = str(trace)
        for forbidden in ("api_key", "password", "credential", "authorization", "bearer"):
            assert forbidden not in serialized.lower()
        # No raw prompt template/model-response text field exists anywhere
        # on AITraceResult/AITraceUsageEntry — structurally, not just by
        # accident of this particular content.
        assert not hasattr(trace, "raw_prompt")
        assert not hasattr(trace, "raw_response")
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

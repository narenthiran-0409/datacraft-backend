"""Phase 4.9 — end-to-end proof that a real generate_corrections() call
actually produces a complete, traceable chain (CorrectionSuggestion ->
AISuggestion -> AIUsageLog, with ai_usage_logs.ai_suggestion_id populated
— the exact gap this phase closes), exercised through the real HTTP
route. Provider always mocked — zero real external LLM calls.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    AIPromptVersion,
    AIUsageLog,
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    Schema,
    User,
)
from app.modules.ai.providers import ProviderResponse
from app.modules.ai.suggestion_service import AISuggestionService


def _create_prompt_version(db: Session, admin_user: User) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key="ai_correction", version_number=4, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_correction_provider(*, category="AI_HIGH_CONFIDENCE", suggested_value="fixed-value", confidence=0.9):
    payload = json.dumps(
        {"category": category, "suggested_value": suggested_value, "confidence": confidence, "reasoning": "n/a"}
    )
    fake_response = ProviderResponse(
        content=payload, input_tokens=120, output_tokens=45, latency_ms=310, raw_metadata={}
    )
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name):
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
        actor=admin_user, name=f"p49_ep_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
        validation_run_id=validation_run.id, name=f"p49_ep_{uuid.uuid4().hex[:6]}", actor=admin_user
    )
    db.expire_all()
    return review_run


def test_real_correction_generates_complete_traceable_chain_via_api(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch, client, admin_headers,
) -> None:
    table_name = f"dq_p49_e2e_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_correction_provider()}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.commit()
        db.expire_all()

        cs = db.execute(
            select(CorrectionSuggestion)
            .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
            .where(Issue.review_run_id == review_run.id)
        ).scalar_one()
        assert cs.ai_suggestion_id is not None

        # THE Phase 4.9 fix: the usage log written during that real call
        # is now actually linked, not left permanently NULL.
        usage_log = db.execute(
            select(AIUsageLog).where(AIUsageLog.ai_suggestion_id == cs.ai_suggestion_id)
        ).scalar_one()
        assert usage_log.provider == "anthropic"
        assert usage_log.input_tokens == 120
        assert usage_log.output_tokens == 45

        response = client.get(f"/api/v1/suggestions/{cs.id}/ai-trace", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["correction_suggestion_id"] == str(cs.id)
        assert body["ai_suggestion_id"] == str(cs.ai_suggestion_id)
        assert body["is_llm_backed"] is True
        assert body["linkage_status"] == "OK"
        assert body["provider"] == "anthropic"
        assert body["prompt"]["key"] == "ai_correction"
        assert body["prompt"]["version_number"] == 4
        assert len(body["usage"]) == 1
        assert body["usage"][0]["id"] == str(usage_log.id)
        assert body["usage"][0]["input_tokens"] == 120
        assert body["usage"][0]["output_tokens"] == 45
        assert body["usage"][0]["total_tokens"] == 165
        assert body["usage"][0]["status"] == "SUCCESS"
        # Never raw prompt/response text anywhere in the response.
        assert "raw_prompt" not in body
        assert "raw_response" not in body
        assert "template" not in body
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_failed_llm_call_usage_log_is_still_backfilled_and_traceable(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch,
) -> None:
    """A real provider call that fails (AIProviderUnavailableError) still
    writes a usage log — and Phase 4.9's backfill still links it to the
    resulting (degraded CANNOT_INFER) suggestion, so the audit trail
    proves an attempt was genuinely made rather than silently vanishing."""
    from app.core.exceptions import AIProviderUnavailableError

    table_name = f"dq_p49_e2e_fail_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        mock_cls = MagicMock()
        mock_cls.return_value.send.side_effect = AIProviderUnavailableError("simulated failure")
        mock_cls.return_value.name = "anthropic"

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.commit()
        db.expire_all()

        cs = db.execute(
            select(CorrectionSuggestion)
            .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
            .where(Issue.review_run_id == review_run.id)
        ).scalar_one()
        assert cs.category == "CANNOT_INFER"
        assert cs.ai_suggestion_id is not None

        usage_log = db.execute(
            select(AIUsageLog).where(AIUsageLog.ai_suggestion_id == cs.ai_suggestion_id)
        ).scalar_one()
        assert usage_log.provider == "anthropic"  # the REAL provider that was attempted
        assert usage_log.input_tokens is None
        assert usage_log.output_tokens is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_trace_endpoint_requires_review_read_permission(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, client, no_role_headers,
) -> None:
    table_name = f"dq_p49_perm_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalar_one()
        cs = CorrectionSuggestion(
            issue_id=issue.id, source="RULE_BASED", ai_suggestion_id=None, suggested_value="x",
            confidence="1.0", category="DETERMINISTIC", fix_type="RULE_PROPOSED", reasoning=None, is_selected=False,
        )
        db.add(cs)
        db.commit()

        response = client.get(f"/api/v1/suggestions/{cs.id}/ai-trace", headers=no_role_headers)
        assert response.status_code == 403
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_trace_endpoint_missing_suggestion_returns_404(client, admin_headers) -> None:
    response = client.get(f"/api/v1/suggestions/{uuid.uuid4()}/ai-trace", headers=admin_headers)
    assert response.status_code == 404


def test_reading_ai_trace_never_calls_the_provider(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch, client, admin_headers,
) -> None:
    table_name = f"dq_p49_noai_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        mock_cls = _mock_correction_provider()
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.commit()
        db.expire_all()
        call_count_after_generation = mock_cls.return_value.send.call_count

        cs = db.execute(
            select(CorrectionSuggestion)
            .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
            .where(Issue.review_run_id == review_run.id)
        ).scalar_one()

        # Reading the trace twice must never call the provider again.
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
            client.get(f"/api/v1/suggestions/{cs.id}/ai-trace", headers=admin_headers)
            client.get(f"/api/v1/suggestions/{cs.id}/ai-trace", headers=admin_headers)

        assert mock_cls.return_value.send.call_count == call_count_after_generation
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

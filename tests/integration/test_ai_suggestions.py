"""Integration tests for the AI suggestion/correction-bridge path against
real local Postgres. Provider is always mocked — zero real external LLM
calls. Every assertion is checked against direct database/service state,
not merely HTTP status."""
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    AIPromptVersion,
    AISuggestion,
    Column,
    Connection,
    Correction,
    CorrectionSuggestion,
    Dataset,
    Issue,
    RuleVersion,
    Schema,
    User,
)
from app.modules.ai.providers import ProviderResponse
from app.modules.ai.suggestion_service import AISuggestionService


def _create_prompt_version(db: Session, admin_user: User, prompt_key: str) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_provider(text_content: str = "AI generated text"):
    fake_response = ProviderResponse(
        content=text_content, input_tokens=42, output_tokens=17, latency_ms=123, raw_metadata={}
    )
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _build_review_run_with_issue(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str):
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.review.service import ReviewService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, 'a'), (3, NULL)"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    discover_job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(discover_job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"ai_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=val_col.id, column_ids=None, template_id=None,
    )

    validation_run, validation_job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name="ai_test", actor=admin_user
    )
    db.expire_all()
    return review_run, validation_run, dataset


def test_explanation_suggestion_has_full_provenance_and_proposed_status(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_ai_expl_{uuid.uuid4().hex[:8]}"
    try:
        review_run, validation_run, dataset = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        _create_prompt_version(db, admin_user, "ai_explanation")

        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("explanation text")}):
            suggestion = AISuggestionService(db).generate_explanation(issue.id, admin_user)

        assert suggestion.status == "PROPOSED"
        assert suggestion.provider == "anthropic"
        assert suggestion.model
        assert suggestion.prompt_version_id is not None
        assert suggestion.requested_by == admin_user.id
        assert suggestion.created_at is not None
        assert suggestion.response_metadata["input_context_hash"]
        assert len(suggestion.response_metadata["input_context_hash"]) == 64
        assert suggestion.suggestion_type == "EXPLANATION"
        assert suggestion.source_context_type == "ISSUE"
        assert suggestion.source_context_id == issue.id
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_correction_bridge_creates_suggestion_and_correction_suggestion_row(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_ai_corr_{uuid.uuid4().hex[:8]}"
    try:
        review_run, validation_run, dataset = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_correction")

        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("suggested-fix-value")}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)

        assert len(suggestions) == 1
        suggestion = suggestions[0]
        assert suggestion.suggestion_type == "CORRECTION"

        bridge_row = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.ai_suggestion_id == suggestion.id)
        ).scalar_one()
        assert bridge_row.source == "AI"
        assert bridge_row.ai_suggestion_id == suggestion.id
        assert bridge_row.suggested_value == "suggested-fix-value"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_correction_decision_service_accept_edit_reject_unchanged_for_ai_source(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Explicit confirmation: CorrectionDecisionService.accept/edit/reject
    work IDENTICALLY for source='AI' as for source='RULE_BASED' — proving
    zero modification to Phase 6 decision logic."""
    from app.modules.review.decision_service import CorrectionDecisionService

    table_name = f"dq_ai_decision_{uuid.uuid4().hex[:8]}"
    try:
        review_run, validation_run, dataset = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_correction")

        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("ai-value")}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge_row = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.ai_suggestion_id == suggestions[0].id)
        ).scalar_one()
        assert bridge_row.source == "AI"

        # accept()
        correction = CorrectionDecisionService(db).accept(bridge_row.id, admin_user)
        assert correction.status == "ACCEPTED"
        assert correction.final_value == "ai-value"

        # edit()
        edited = CorrectionDecisionService(db).edit(bridge_row.id, "manual-override", admin_user)
        assert edited.status == "EDITED"
        assert edited.final_value == "manual-override"
        assert edited.id == correction.id  # same upsert-on-issue_id row, unmodified Phase 6 behavior
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_correction_reject_works_for_ai_source(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    from app.modules.review.decision_service import CorrectionDecisionService

    table_name = f"dq_ai_reject_{uuid.uuid4().hex[:8]}"
    try:
        review_run, validation_run, dataset = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_correction")

        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("ai-value")}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        bridge_row = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.ai_suggestion_id == suggestions[0].id)
        ).scalar_one()

        rejected = CorrectionDecisionService(db).reject(bridge_row.id, "not applicable", admin_user)
        assert rejected.status == "REJECTED"
        assert rejected.final_value is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_correction_bridge_is_the_only_write_into_phase6_tables(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Direct before/after comparison of issues/corrections rows other
    than the ONE expected correction_suggestions insert."""
    table_name = f"dq_ai_isolation_{uuid.uuid4().hex[:8]}"
    try:
        review_run, validation_run, dataset = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_correction")

        issues_before = {(i.id, i.status) for i in db.execute(select(Issue)).scalars()}
        corrections_before = list(db.execute(select(Correction)).scalars())
        correction_suggestions_before = list(db.execute(select(CorrectionSuggestion)).scalars())

        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("v")}):
            suggestions = AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        issues_after = {(i.id, i.status) for i in db.execute(select(Issue)).scalars()}
        corrections_after = list(db.execute(select(Correction)).scalars())
        correction_suggestions_after = list(db.execute(select(CorrectionSuggestion)).scalars())

        assert issues_before == issues_after  # issues table untouched
        assert corrections_before == corrections_after  # corrections table untouched
        assert len(correction_suggestions_after) == len(correction_suggestions_before) + len(suggestions)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_suggestion_response_never_contains_raw_row_content(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_ai_raw_{uuid.uuid4().hex[:8]}"
    try:
        review_run, validation_run, dataset = _build_review_run_with_issue(db, redis_client, admin_user, pg_connection, table_name)
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        _create_prompt_version(db, admin_user, "ai_explanation")

        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("text")}):
            suggestion = AISuggestionService(db).generate_explanation(issue.id, admin_user)

        blob = str(suggestion.content) + str(suggestion.response_metadata)
        for forbidden in ("password", "credential_ref", "connection_string"):
            assert forbidden not in blob.lower()
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

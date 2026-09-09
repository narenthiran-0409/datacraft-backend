"""Integration tests for the AI_SUGGESTION Celery tasks against real local
Postgres — direct task invocation (matching this project's own convention:
"The Celery task is invoked directly, not via .delay()/a broker, for
automated tests"). Provider always mocked."""
import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    AIPromptVersion,
    AISuggestion,
    Column,
    Connection,
    Job,
    Schema,
    User,
)
from app.modules.ai.providers import ProviderResponse
from app.modules.ai.tasks import run_ai_run_summary
from app.modules.jobs.service import JobsService


def _create_prompt_version(db: Session, admin_user: User, prompt_key: str) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_provider(text_content: str = "summary text"):
    fake_response = ProviderResponse(content=text_content, input_tokens=1, output_tokens=1, latency_ms=1, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _validated_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str):
    from app.modules.discovery.tasks import run_discovery
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation
    from app.db.models import RuleVersion

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, NULL)"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    discover_job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(discover_job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    from app.db.models import Dataset
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"ai_task_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
    return validation_run


def test_run_ai_run_summary_task_creates_suggestion_and_completes_job(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_ai_task_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _validated_dataset(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_run_summary")
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        job = JobsService(db, redis_client).create(
            job_type="AI_SUGGESTION", entity_type="VALIDATION_RUN", entity_id=validation_run.id,
            created_by=admin_user.id,
        )

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("run summary text")}):
            result = run_ai_run_summary(str(job.id), str(validation_run.id))

        assert result["status"] == "COMPLETED"
        db.expire_all()
        refreshed_job = db.get(Job, job.id)
        assert refreshed_job.status == "COMPLETED"

        suggestion = db.get(AISuggestion, uuid.UUID(result["ai_suggestion_id"]))
        assert suggestion.suggestion_type == "RUN_SUMMARY"
        assert suggestion.status == "PROPOSED"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_run_ai_task_duplicate_delivery_is_idempotent(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_ai_task_dup_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _validated_dataset(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_run_summary")
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        job = JobsService(db, redis_client).create(
            job_type="AI_SUGGESTION", entity_type="VALIDATION_RUN", entity_id=validation_run.id,
            created_by=admin_user.id,
        )

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("first")}):
            first = run_ai_run_summary(str(job.id), str(validation_run.id))
        assert first["status"] == "COMPLETED"

        # Simulated redelivery of the exact same Celery message after COMPLETED.
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("second")}):
            second = run_ai_run_summary(str(job.id), str(validation_run.id))
        assert second == {"status": "COMPLETED"}  # idempotency guard early-return, no second suggestion created

        db.expire_all()
        suggestions = db.execute(
            select(AISuggestion).where(AISuggestion.source_context_id == validation_run.id)
        ).scalars().all()
        assert len(suggestions) == 1  # unchanged by the redelivery
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_run_ai_task_failure_marks_job_failed(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_ai_task_fail_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _validated_dataset(db, redis_client, admin_user, pg_connection, table_name)
        # Deliberately do NOT create an ai_prompt_versions row — the
        # orchestrator will raise AIPromptVersionNotFoundError.
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        job = JobsService(db, redis_client).create(
            job_type="AI_SUGGESTION", entity_type="VALIDATION_RUN", entity_id=validation_run.id,
            created_by=admin_user.id,
        )

        result = run_ai_run_summary(str(job.id), str(validation_run.id))
        assert result["status"] == "FAILED"
        assert "prompt version" in result["error"].lower()

        db.expire_all()
        refreshed_job = db.get(Job, job.id)
        assert refreshed_job.status == "FAILED"
        assert refreshed_job.error_message is not None
        assert "AIPromptVersionNotFoundError" in refreshed_job.error_message  # jobs.error_message includes the type name

        suggestions = db.execute(
            select(AISuggestion).where(AISuggestion.source_context_id == validation_run.id)
        ).scalars().all()
        assert suggestions == []  # no partial/fabricated suggestion on failure
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_run_ai_task_retry_behavior_on_transient_provider_error(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """AnthropicProvider itself retries transient timeouts internally
    (see test_ai_provider.py); this confirms the task layer surfaces an
    eventual success after the provider-level retry succeeds."""
    table_name = f"dq_ai_task_retry_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _validated_dataset(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user, "ai_run_summary")
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        job = JobsService(db, redis_client).create(
            job_type="AI_SUGGESTION", entity_type="VALIDATION_RUN", entity_id=validation_run.id,
            created_by=admin_user.id,
        )

        import anthropic

        # Patches the underlying Anthropic SDK client (not the orchestrator's
        # provider registry) so AnthropicProvider's own internal retry loop
        # is exercised for real, mirroring test_ai_provider.py's approach.
        with patch("app.modules.ai.providers.anthropic_provider.anthropic.Anthropic") as MockSDKClient:
            MockSDKClient.return_value.messages.create.side_effect = [
                anthropic.APITimeoutError(request=MagicMock()),
                _fake_sdk_response("recovered after retry"),
            ]
            result = run_ai_run_summary(str(job.id), str(validation_run.id))

        assert result["status"] == "COMPLETED"
        db.expire_all()
        suggestion = db.get(AISuggestion, uuid.UUID(result["ai_suggestion_id"]))
        assert suggestion.content["text"] == "recovered after retry"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def _fake_sdk_response(text_content: str):
    block = MagicMock()
    block.type = "text"
    block.text = text_content
    response = MagicMock()
    response.content = [block]
    response.usage = MagicMock(input_tokens=1, output_tokens=1)
    response.stop_reason = "end_turn"
    response.model = "claude-test"
    return response

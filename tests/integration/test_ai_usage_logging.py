"""Integration tests for ai_usage_logs writes against real local Postgres.
Provider always mocked."""
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AIPromptVersion, AIUsageLog, User
from app.modules.ai.orchestrator_service import AIOrchestratorService
from app.modules.ai.providers import ProviderResponse


def _create_prompt_version(db: Session, admin_user: User, prompt_key: str) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def test_successful_call_writes_one_usage_log_with_token_counts(db: Session, admin_user: User, monkeypatch) -> None:
    _create_prompt_version(db, admin_user, "usage_test")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    fake_response = ProviderResponse(content="ok", input_tokens=100, output_tokens=50, latency_ms=250, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
        AIOrchestratorService(db).run(prompt_key="usage_test", context={"x": 1}, actor=admin_user)
    db.commit()

    logs = db.execute(select(AIUsageLog).where(AIUsageLog.user_id == admin_user.id)).scalars().all()
    assert len(logs) == 1
    assert logs[0].input_tokens == 100
    assert logs[0].output_tokens == 50
    assert logs[0].latency_ms == 250
    assert logs[0].provider == "anthropic"
    assert logs[0].cost_estimate is None  # never fabricated — provider doesn't return cost


def test_missing_token_counts_stored_as_null_not_fabricated(db: Session, admin_user: User, monkeypatch) -> None:
    _create_prompt_version(db, admin_user, "usage_null_test")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    fake_response = ProviderResponse(content="ok", input_tokens=None, output_tokens=None, latency_ms=10, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
        AIOrchestratorService(db).run(prompt_key="usage_null_test", context={}, actor=admin_user)
    db.commit()

    logs = db.execute(select(AIUsageLog).where(AIUsageLog.user_id == admin_user.id)).scalars().all()
    assert len(logs) == 1
    assert logs[0].input_tokens is None
    assert logs[0].output_tokens is None


def test_failed_call_still_writes_a_usage_log(db: Session, admin_user: User, monkeypatch) -> None:
    from app.core.exceptions import AIProviderUnavailableError

    _create_prompt_version(db, admin_user, "usage_fail_test")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    mock_cls = MagicMock()
    mock_cls.return_value.send.side_effect = AIProviderUnavailableError("boom")
    mock_cls.return_value.name = "anthropic"

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_cls}):
        try:
            AIOrchestratorService(db).run(prompt_key="usage_fail_test", context={}, actor=admin_user)
        except AIProviderUnavailableError:
            pass
    db.commit()

    logs = db.execute(select(AIUsageLog).where(AIUsageLog.user_id == admin_user.id)).scalars().all()
    assert len(logs) == 1
    assert logs[0].input_tokens is None
    assert logs[0].output_tokens is None

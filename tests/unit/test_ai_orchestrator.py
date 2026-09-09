"""Unit-adjacent tests for AIOrchestratorService (real Postgres for the
prompt-version lookup, mocked provider throughout — zero real external
LLM calls) and the deterministic context-hashing utility."""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AIDisabledError, AIPromptVersionNotFoundError
from app.db.models import AIPromptVersion, User
from app.modules.ai.context import compute_input_context_hash
from app.modules.ai.orchestrator_service import AIOrchestratorService
from app.modules.ai.providers import ProviderResponse


def _create_prompt_version(db: Session, admin_user: User, prompt_key: str, *, is_active: bool = True) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="You are a helpful assistant.",
        default_model="claude-test", is_active=is_active, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


# --- AI_ENABLED gating -------------------------------------------------


def test_ai_disabled_raises_before_any_provider_call(db: Session, admin_user: User, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    orchestrator = AIOrchestratorService(db)

    with pytest.raises(AIDisabledError):
        orchestrator.run(prompt_key="does_not_matter", context={}, actor=admin_user)


def test_ai_enabled_false_never_reaches_prompt_resolution(db: Session, admin_user: User, monkeypatch) -> None:
    """Confirms the AI_ENABLED check happens strictly before prompt
    resolution — using a prompt_key with NO active version, expecting
    AIDisabledError (not AIPromptVersionNotFoundError) to prove ordering."""
    monkeypatch.setattr(settings, "AI_ENABLED", False)
    orchestrator = AIOrchestratorService(db)

    with pytest.raises(AIDisabledError):
        orchestrator.run(prompt_key="nonexistent_prompt_key", context={}, actor=admin_user)


# --- prompt version resolution -------------------------------------------


def test_prompt_version_resolution_picks_active_version(db: Session, admin_user: User, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    _create_prompt_version(db, admin_user, "greet")

    fake_response = ProviderResponse(content="hi", input_tokens=1, output_tokens=1, latency_ms=5, raw_metadata={})
    mock_provider_cls = MagicMock()
    mock_provider_cls.return_value.send.return_value = fake_response
    mock_provider_cls.return_value.name = "anthropic"
    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_provider_cls}):
        orchestrator = AIOrchestratorService(db)
        result = orchestrator.run(prompt_key="greet", context={"a": 1}, actor=admin_user)

    assert result.prompt_version.prompt_key == "greet"
    assert result.text == "hi"


def test_prompt_version_missing_raises_not_found(db: Session, admin_user: User, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    orchestrator = AIOrchestratorService(db)

    with pytest.raises(AIPromptVersionNotFoundError):
        orchestrator.run(prompt_key="never_seeded", context={}, actor=admin_user)


def test_inactive_prompt_version_not_selected(db: Session, admin_user: User, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    _create_prompt_version(db, admin_user, "inactive_key", is_active=False)
    orchestrator = AIOrchestratorService(db)

    with pytest.raises(AIPromptVersionNotFoundError):
        orchestrator.run(prompt_key="inactive_key", context={}, actor=admin_user)


# --- provider/model selection from config -------------------------------


def test_provider_model_selection_uses_prompt_default_when_no_override(db: Session, admin_user: User, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    _create_prompt_version(db, admin_user, "model_test")

    fake_response = ProviderResponse(content="ok", input_tokens=1, output_tokens=1, latency_ms=1, raw_metadata={})
    mock_provider_cls = MagicMock()
    mock_provider_cls.return_value.send.return_value = fake_response
    mock_provider_cls.return_value.name = "anthropic"
    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_provider_cls}):
        orchestrator = AIOrchestratorService(db)
        result = orchestrator.run(prompt_key="model_test", context={}, actor=admin_user)

    assert result.model == "claude-test"  # the prompt version's default_model


# --- forbidden-context exclusion -----------------------------------------


def test_context_never_contains_credentials_or_raw_row_keys() -> None:
    from app.modules.ai.context import build_issue_context

    class FakeIssue:
        severity = "HIGH"
        status = "PENDING"

    class FakeColumn:
        name = "email"
        normalized_data_type = "STRING"

    class FakeFailure:
        severity = "HIGH"
        reason = "null value"

    class FakeRule:
        rule_type = "COMPLETENESS"
        category = None

    class FakeRuleVersion:
        definition = {"max_null_percentage": 0}
        severity = "HIGH"

    context = build_issue_context(
        issue=FakeIssue(), column=FakeColumn(), validation_failure=FakeFailure(),
        rule=FakeRule(), rule_version=FakeRuleVersion(),
    )
    serialized = str(context)
    for forbidden in ("password", "credential", "connection_string", "api_key", "original_value", "failed_value"):
        assert forbidden not in serialized.lower()


# --- deterministic SHA-256 context hashing --------------------------------


def test_context_hash_deterministic_across_key_order() -> None:
    h1 = compute_input_context_hash({"b": 1, "a": {"z": 1, "y": 2}})
    h2 = compute_input_context_hash({"a": {"y": 2, "z": 1}, "b": 1})
    assert h1 == h2
    assert len(h1) == 64  # sha256 hex digest length


def test_context_hash_differs_for_different_content() -> None:
    h1 = compute_input_context_hash({"a": 1})
    h2 = compute_input_context_hash({"a": 2})
    assert h1 != h2


def test_context_hash_repeated_calls_same_process_are_stable() -> None:
    context = {"issue_id": str(uuid.uuid4()), "severity": "HIGH"}
    hashes = {compute_input_context_hash(context) for _ in range(5)}
    assert len(hashes) == 1

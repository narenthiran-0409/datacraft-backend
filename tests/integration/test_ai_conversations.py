"""Integration tests for AI chat/conversation persistence against real
local Postgres. Provider always mocked — zero real external LLM calls."""
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AIConversationNotFoundError
from app.db.models import AIMessage, AIPromptVersion, User
from app.modules.ai.chat_service import AIChatService
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


def _mock_provider(text_content: str):
    fake_response = ProviderResponse(content=text_content, input_tokens=5, output_tokens=5, latency_ms=10, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def test_send_message_creates_conversation_and_two_messages(db: Session, admin_user: User, monkeypatch) -> None:
    _create_prompt_version(db, admin_user, "ai_chat")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("assistant reply")}):
        conversation, assistant_message = AIChatService(db).send_message(
            conversation_id=None, message="hello", actor=admin_user
        )

    assert conversation.user_id == admin_user.id
    assert assistant_message.role == "ASSISTANT"
    assert assistant_message.content == "assistant reply"

    messages = db.execute(
        select(AIMessage).where(AIMessage.conversation_id == conversation.id).order_by(AIMessage.created_at)
    ).scalars().all()
    assert len(messages) == 2
    assert messages[0].role == "USER"
    assert messages[0].content == "hello"
    assert messages[1].role == "ASSISTANT"


def test_send_message_continues_existing_conversation(db: Session, admin_user: User, monkeypatch) -> None:
    _create_prompt_version(db, admin_user, "ai_chat")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("first")}):
        conversation, _ = AIChatService(db).send_message(conversation_id=None, message="hi", actor=admin_user)

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("second")}):
        conversation2, _ = AIChatService(db).send_message(
            conversation_id=conversation.id, message="follow up", actor=admin_user
        )

    assert conversation2.id == conversation.id
    messages = db.execute(
        select(AIMessage).where(AIMessage.conversation_id == conversation.id).order_by(AIMessage.created_at)
    ).scalars().all()
    assert len(messages) == 4


def test_get_conversation_returns_full_history(db: Session, admin_user: User, monkeypatch) -> None:
    _create_prompt_version(db, admin_user, "ai_chat")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("reply")}):
        conversation, _ = AIChatService(db).send_message(conversation_id=None, message="q", actor=admin_user)

    fetched_conversation, messages = AIChatService(db).get_conversation(conversation.id, admin_user)
    assert fetched_conversation.id == conversation.id
    assert len(messages) == 2


def test_get_conversation_wrong_user_raises_not_found(db: Session, admin_user: User, monkeypatch) -> None:
    from tests.conftest import _create_user_with_role

    _create_prompt_version(db, admin_user, "ai_chat")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("reply")}):
        conversation, _ = AIChatService(db).send_message(conversation_id=None, message="q", actor=admin_user)

    other_user = _create_user_with_role(db, "analyst", email="ai_other@example.com")
    with pytest.raises(AIConversationNotFoundError):
        AIChatService(db).get_conversation(conversation.id, other_user)


def test_conversation_content_never_contains_credentials(db: Session, admin_user: User, monkeypatch) -> None:
    _create_prompt_version(db, admin_user, "ai_chat")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "super-secret-api-key-value")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider("normal reply")}):
        conversation, _ = AIChatService(db).send_message(conversation_id=None, message="hello", actor=admin_user)

    messages = db.execute(select(AIMessage).where(AIMessage.conversation_id == conversation.id)).scalars().all()
    blob = " ".join(m.content for m in messages)
    assert "super-secret-api-key-value" not in blob

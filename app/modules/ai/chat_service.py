import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import AIConversationNotFoundError
from app.db.models import AIConversation, AIMessage, User
from app.modules.ai.orchestrator_service import AIOrchestratorService

_CHAT_PROMPT_KEY = "ai_chat"


class AIChatService:
    """Synchronous conversational Q&A. Never automatically creates a
    correction or modifies any authoritative table — chat is advisory
    only, exactly like every other AI output in this implementation."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._orchestrator = AIOrchestratorService(db)

    def get_conversation(self, conversation_id: uuid.UUID, actor: User) -> tuple[AIConversation, list[AIMessage]]:
        conversation = self._db.get(AIConversation, conversation_id)
        if conversation is None or conversation.user_id != actor.id:
            raise AIConversationNotFoundError(f"Conversation {conversation_id} not found")
        messages = list(
            self._db.query(AIMessage)
            .filter(AIMessage.conversation_id == conversation.id)
            .order_by(AIMessage.created_at)
        )
        return conversation, messages

    def send_message(self, *, conversation_id: uuid.UUID | None, message: str, actor: User) -> tuple[AIConversation, AIMessage]:
        if conversation_id is not None:
            conversation = self._db.get(AIConversation, conversation_id)
            if conversation is None or conversation.user_id != actor.id:
                raise AIConversationNotFoundError(f"Conversation {conversation_id} not found")
        else:
            conversation = AIConversation(user_id=actor.id, status="ACTIVE")
            self._db.add(conversation)
            self._db.flush()

        user_message = AIMessage(conversation_id=conversation.id, role="USER", content=message)
        self._db.add(user_message)
        self._db.flush()

        result = self._orchestrator.run(
            prompt_key=_CHAT_PROMPT_KEY, context={"message": message}, actor=actor, conversation_id=conversation.id,
        )

        assistant_message = AIMessage(conversation_id=conversation.id, role="ASSISTANT", content=result.text)
        self._db.add(assistant_message)
        conversation.updated_at = datetime.now(timezone.utc)
        self._db.commit()
        self._db.refresh(assistant_message)
        return conversation, assistant_message

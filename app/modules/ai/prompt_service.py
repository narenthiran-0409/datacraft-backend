from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import AIPromptVersionNotFoundError
from app.db.models import AIPromptVersion


class PromptVersionService:
    """The single chokepoint every prompt-driven AI call resolves its
    template through — no business prompt text is ever hard-coded inside
    a service file. No prompt-seeding logic lives here; initial
    ai_prompt_versions rows are populated by a separate, future,
    controlled seed step (deferred, not part of this implementation)."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve_active(self, prompt_key: str) -> AIPromptVersion:
        version = self._db.execute(
            select(AIPromptVersion)
            .where(AIPromptVersion.prompt_key == prompt_key, AIPromptVersion.is_active.is_(True))
            .order_by(AIPromptVersion.version_number.desc())
        ).scalars().first()
        if version is None:
            raise AIPromptVersionNotFoundError(f"No active prompt version found for prompt_key={prompt_key!r}")
        return version

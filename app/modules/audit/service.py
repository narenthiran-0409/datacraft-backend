import uuid

from sqlalchemy.orm import Session

from app.db.models import AuditEvent, User


class AuditingService:
    """Records audit_events rows. Callers are responsible for committing the
    same transaction that also persists the state change being documented."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def record(
        self,
        *,
        actor: User | None,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID,
        before: dict | None = None,
        after: dict | None = None,
        metadata: dict | None = None,
        actor_type: str = "USER",
    ) -> AuditEvent:
        event = AuditEvent(
            actor_id=actor.id if actor is not None else None,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before_value=before,
            after_value=after,
            audit_metadata=metadata,
        )
        self._db.add(event)
        self._db.flush()
        return event

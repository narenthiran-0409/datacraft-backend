import uuid
from datetime import datetime

from sqlalchemy import Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class LineageRecord(UUIDPKMixin, Base):
    """Frozen shape (Phase 10): no new column, no new constraint may ever be
    added. entity_type/relationship_type columns are deliberately
    unconstrained (no CHECK) so Decision 2's vocabulary extensions
    (PROFILE_RUN, REVIEW_RUN, PROFILED_BY, ...) never require a migration."""

    __tablename__ = "lineage_records"

    parent_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    parent_entity_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    child_entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    child_entity_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

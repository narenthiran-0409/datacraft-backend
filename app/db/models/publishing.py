import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class PublishRun(UUIDPKMixin, Base):
    __tablename__ = "publish_runs"

    staging_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("staging_runs.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING", server_default="PENDING")
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_record_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    drift_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    drift_acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    drift_acknowledged_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)

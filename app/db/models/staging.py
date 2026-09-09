import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class StagingRun(UUIDPKMixin, Base):
    __tablename__ = "staging_runs"
    __table_args__ = (
        UniqueConstraint("review_run_id", "attempt_number", name="uq_staging_runs_review_run_id_attempt_number"),
    )

    review_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("review_runs.id"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="NOT_STARTED", server_default="NOT_STARTED")
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    field_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    has_source_drift: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class StagingRecord(UUIDPKMixin, Base):
    __tablename__ = "staging_records"
    __table_args__ = (
        UniqueConstraint("staging_run_id", "record_ref", name="uq_staging_records_staging_run_id_record_ref"),
    )

    staging_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("staging_runs.id", ondelete="CASCADE"), nullable=False
    )
    record_ref: Mapped[str] = mapped_column(Text, nullable=False)
    row_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    corrected_fields: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    source_row_hash_at_validation: Mapped[str] = mapped_column(Text, nullable=False)
    source_row_hash_at_staging: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_drift_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="UNCHANGED", server_default="UNCHANGED"
    )
    source_drift_fields: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

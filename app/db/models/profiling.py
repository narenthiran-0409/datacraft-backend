import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class ProfileRun(UUIDPKMixin, Base):
    __tablename__ = "profile_runs"

    dataset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="QUEUED", server_default="QUEUED")
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    null_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    duplicate_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class ColumnProfile(UUIDPKMixin, Base):
    __tablename__ = "column_profiles"
    __table_args__ = (UniqueConstraint("profile_run_id", "column_id", name="uq_column_profiles_profile_run_id_column_id"),)

    profile_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("profile_runs.id", ondelete="CASCADE"), nullable=False
    )
    column_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("columns.id"), nullable=False)
    null_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    null_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    distinct_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    distinct_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    duplicate_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duplicate_percentage: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    min_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    mean_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    median_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    mode_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_length: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    outlier_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pattern_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    value_distribution: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    # Amendment (d), approved for Phase 4.
    stddev_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    sum_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)

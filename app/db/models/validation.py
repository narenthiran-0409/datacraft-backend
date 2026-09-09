import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class ValidationRun(UUIDPKMixin, Base):
    __tablename__ = "validation_runs"

    dataset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_templates.id"), nullable=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="CREATED", server_default="CREATED")
    sample_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    passed_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    warning_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    failed_rows: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ValidationResult(UUIDPKMixin, Base):
    __tablename__ = "validation_results"
    __table_args__ = (
        UniqueConstraint("validation_run_id", "row_index", name="uq_validation_results_run_id_row_index"),
    )

    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False
    )
    record_ref: Mapped[str] = mapped_column(Text, nullable=False)
    row_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    source_row_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class ValidationFailure(UUIDPKMixin, Base):
    __tablename__ = "validation_failures"

    validation_result_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_results.id", ondelete="CASCADE"), nullable=False
    )
    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False
    )
    rule_assignment_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rule_assignments.id"), nullable=False
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("columns.id"), nullable=True
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    failed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class ValidationMetric(UUIDPKMixin, Base):
    __tablename__ = "validation_metrics"
    __table_args__ = (
        UniqueConstraint(
            "validation_run_id", "metric_name", "metric_group", name="uq_validation_metrics_run_name_group"
        ),
    )

    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_runs.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    metric_group: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_value: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class ReviewRun(UUIDPKMixin, Base):
    __tablename__ = "review_runs"

    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_runs.id"), nullable=False
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="DRAFT", server_default="DRAFT")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Issue(UUIDPKMixin, Base):
    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("review_run_id", "validation_failure_id", name="uq_issues_review_run_id_validation_failure_id"),
    )

    review_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("review_runs.id", ondelete="CASCADE"), nullable=False
    )
    validation_failure_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_failures.id"), nullable=False
    )
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("columns.id"), nullable=True
    )
    record_ref: Mapped[str] = mapped_column(Text, nullable=False)
    row_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING", server_default="PENDING")
    assigned_reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class CorrectionSuggestion(UUIDPKMixin, Base):
    __tablename__ = "correction_suggestions"

    issue_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # FK attached by migration 0016_phase12_ai_foundation (constraint name
    # fk_correction_suggestions_ai_suggestion_id) — deferred since Phase 6
    # because ai_suggestions did not exist until the AI Orchestrator phase.
    # Column itself is unchanged: still nullable, still the same shape.
    ai_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ai_suggestions.id"), nullable=True
    )
    suggested_value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(nullable=False)
    fix_type: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_selected: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    selected_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    selected_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class Correction(UUIDPKMixin, Base):
    __tablename__ = "corrections"
    __table_args__ = (UniqueConstraint("issue_id", name="uq_corrections_issue_id"),)

    issue_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    correction_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("correction_suggestions.id"), nullable=True
    )
    final_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)

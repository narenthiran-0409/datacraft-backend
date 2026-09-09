import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class ApprovalRequest(UUIDPKMixin, Base):
    __tablename__ = "approval_requests"

    review_run_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("review_runs.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING", server_default="PENDING")
    affected_issue_count: Mapped[int] = mapped_column(Integer, nullable=False)
    affected_record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    requested_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ApprovalDecision(UUIDPKMixin, Base):
    __tablename__ = "approval_decisions"

    approval_request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("approval_requests.id", ondelete="CASCADE"), nullable=False
    )
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class ApprovalDecisionIssue(Base):
    __tablename__ = "approval_decision_issues"

    approval_decision_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("approval_decisions.id", ondelete="CASCADE"), primary_key=True
    )
    issue_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("issues.id"), primary_key=True
    )

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import UUIDPKMixin


class Rule(UUIDPKMixin, Base):
    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("name", name="uq_rules_name"),)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_type: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="BUILT_IN", server_default="BUILT_IN")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ACTIVE", server_default="ACTIVE")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RuleVersion(UUIDPKMixin, Base):
    __tablename__ = "rule_versions"
    __table_args__ = (UniqueConstraint("rule_id", "version_number", name="uq_rule_versions_rule_id_version_number"),)

    rule_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("rules.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="MEDIUM", server_default="MEDIUM")
    error_message_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_current: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class ValidationTemplate(UUIDPKMixin, Base):
    __tablename__ = "validation_templates"
    __table_args__ = (UniqueConstraint("dataset_id", "name", name="uq_validation_templates_dataset_id_name"),)

    dataset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_runs.id"), nullable=True
    )
    last_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RuleAssignment(UUIDPKMixin, Base):
    __tablename__ = "rule_assignments"

    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rule_versions.id"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    assignment_scope: Mapped[str] = mapped_column(Text, nullable=False)
    column_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("columns.id"), nullable=True
    )
    cross_column_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("validation_templates.id"), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(nullable=False, default=True, server_default="true")
    paused_at: Mapped[datetime | None] = mapped_column(nullable=True)
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    assigned_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class RuleAssignmentColumn(Base):
    __tablename__ = "rule_assignment_columns"

    rule_assignment_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rule_assignments.id", ondelete="CASCADE"), primary_key=True
    )
    column_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("columns.id"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

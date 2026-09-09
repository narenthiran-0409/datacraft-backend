import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPKMixin


class Schema(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "schemas"
    __table_args__ = (UniqueConstraint("connection_id", "name", name="uq_schemas_connection_id_name"),)

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("connections.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    discovered_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class Dataset(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("schema_id", "name", name="uq_datasets_schema_id_name"),)

    schema_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("schemas.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str] = mapped_column(Text, nullable=False, default="TABLE", server_default="TABLE")
    key_strategy: Mapped[str] = mapped_column(
        Text, nullable=False, default="ROW_INDEX_FALLBACK", server_default="ROW_INDEX_FALLBACK"
    )
    row_count_estimate: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_profiled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    discovered_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class Column(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "columns"
    __table_args__ = (UniqueConstraint("dataset_id", "name", name="uq_columns_dataset_id_name"),)

    dataset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("datasets.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal_position: Mapped[int] = mapped_column(Integer, nullable=False)
    native_data_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_data_type: Mapped[str] = mapped_column(Text, nullable=False, default="STRING", server_default="STRING")
    max_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    numeric_precision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    numeric_scale: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_nullable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_primary_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    semantic_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    semantic_category_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # FK attached by migration 0016_phase12_ai_foundation (constraint name
    # fk_columns_semantic_category_ai_suggestion_id) — deferred since Phase 3
    # because ai_suggestions did not exist until the AI Orchestrator phase.
    # Column itself is unchanged: still nullable, still the same shape.
    semantic_category_ai_suggestion_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ai_suggestions.id"), nullable=True
    )
    semantic_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class DatasetKeyColumn(Base):
    __tablename__ = "dataset_key_columns"
    __table_args__ = (UniqueConstraint("dataset_id", "ordinal", name="uq_dataset_key_columns_dataset_id_ordinal"),)

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), primary_key=True
    )
    column_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("columns.id", ondelete="CASCADE"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

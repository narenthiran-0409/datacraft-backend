import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.mixins import TimestampMixin, UUIDPKMixin


class ConnectionType(UUIDPKMixin, Base):
    __tablename__ = "connection_types"
    __table_args__ = (UniqueConstraint("code", name="uq_connection_types_code"),)

    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    driver_module: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())


class DataSource(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "data_sources"
    __table_args__ = (UniqueConstraint("name", name="uq_data_sources_name"),)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    owner_team: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class Connection(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("data_source_id", "name", name="uq_connections_data_source_id_name"),)

    data_source_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("data_sources.id"), nullable=False
    )
    connection_type_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("connection_types.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=True, default=dict, server_default="{}")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="UNKNOWN", server_default="UNKNOWN")
    last_tested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_test_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

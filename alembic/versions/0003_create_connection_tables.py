"""create connection tables

Revision ID: 0003_create_connection_tables
Revises: 0002_create_identity_tables
Create Date: 2026-08-19

Creates connection_types, data_sources, connections exactly per the frozen
Database Design v2, Section 2 (subset used in Phase 2). Hand-written: the
partial index idx_connections_status (status) WHERE is_active is a Postgres
partial index, not reliably produced by --autogenerate, so it is created
explicitly here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0003_create_connection_tables"
down_revision: Union[str, Sequence[str], None] = "0002_create_identity_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "connection_types",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("driver_module", sa.String(150), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("code", name="uq_connection_types_code"),
    )

    op.create_table(
        "data_sources",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("owner_team", sa.String(255), nullable=True),
        sa.Column("business_domain", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("name", name="uq_data_sources_name"),
    )

    op.create_table(
        "connections",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("data_source_id", pg.UUID(as_uuid=True), sa.ForeignKey("data_sources.id"), nullable=False),
        sa.Column("connection_type_id", pg.UUID(as_uuid=True), sa.ForeignKey("connection_types.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database_name", sa.String(255), nullable=True),
        sa.Column("service_name", sa.String(255), nullable=True),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("credential_ref", sa.String(255), nullable=False),
        sa.Column("config", pg.JSONB(), nullable=True, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_latency_ms", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("data_source_id", "name", name="uq_connections_data_source_id_name"),
        sa.CheckConstraint(
            "environment IN ('PROD', 'STAGING', 'DEV', 'DR', 'UNKNOWN')", name="ck_connections_environment"
        ),
        sa.CheckConstraint("status IN ('UNKNOWN', 'HEALTHY', 'WARNING', 'OFFLINE')", name="ck_connections_status"),
    )
    op.execute(
        "CREATE INDEX idx_connections_status ON connections (status) WHERE is_active"
    )
    op.create_index("idx_connections_data_source", "connections", ["data_source_id"])


def downgrade() -> None:
    op.drop_index("idx_connections_data_source", table_name="connections")
    op.execute("DROP INDEX IF EXISTS idx_connections_status")
    op.drop_table("connections")
    op.drop_table("data_sources")
    op.drop_table("connection_types")

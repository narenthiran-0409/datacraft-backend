"""create audit_events table

Revision ID: 0004_create_audit_events_table
Revises: 0003_create_connection_tables
Create Date: 2026-08-19

Creates audit_events exactly per the frozen Database Design v2, Section 10
(AUDIT). Insert-only table for application service accounts; no update/
delete path is exposed anywhere in this codebase.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0004_create_audit_events_table"
down_revision: Union[str, Sequence[str], None] = "0003_create_connection_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("actor_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("actor_type", sa.String(10), nullable=False, server_default="USER"),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("before_value", pg.JSONB(), nullable=True),
        sa.Column("after_value", pg.JSONB(), nullable=True),
        sa.Column("metadata", pg.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("actor_type IN ('USER', 'SYSTEM', 'AI')", name="ck_audit_events_actor_type"),
    )
    op.create_index(
        "idx_audit_events_entity", "audit_events", ["entity_type", "entity_id", sa.text("created_at DESC")]
    )
    op.create_index("idx_audit_events_actor", "audit_events", ["actor_id", sa.text("created_at DESC")])
    op.create_index("idx_audit_events_action", "audit_events", ["action"])
    op.create_index("idx_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("idx_audit_events_created_at", table_name="audit_events")
    op.drop_index("idx_audit_events_action", table_name="audit_events")
    op.drop_index("idx_audit_events_actor", table_name="audit_events")
    op.drop_index("idx_audit_events_entity", table_name="audit_events")
    op.drop_table("audit_events")

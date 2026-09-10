"""Data sources / connections: add deactivated_at column

Revision ID: 0020_deactivated_at_column
Revises: 0019_jobs_result_column
Create Date: 2026-09-10

Follow-up to the reactivate feature. New requirement: a data source or
connection inactive for more than settings.INACTIVE_RECORD_VISIBILITY_DAYS
days must stop appearing in list responses entirely (including "show
inactive" views) — soft-hidden, never hard-deleted, still recoverable via
direct DB/admin access.

is_active alone can't support this: it's a bare boolean with no timestamp
of the deactivation event, and updated_at doesn't reliably stand in for it
either — update_data_source/update_connection and (for connections)
test_connection all bump updated_at independently of deactivation. A
dedicated column is required to know how long a row has been inactive.

Additive only: one nullable TIMESTAMPTZ column on each table, no backfill.
Existing inactive rows (if any) get NULL rather than a fabricated
timestamp — an unknown deactivation time is treated as "can't confirm
recency" by the service-layer visibility filter, so they're conservatively
excluded from list views rather than assigned a guessed age. Application
code (DataSourcesService/ConnectionsService) sets this column going
forward: populated on deactivate, cleared back to NULL on reactivate.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0020_deactivated_at_column"
down_revision: Union[str, Sequence[str], None] = "0019_jobs_result_column"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("data_sources", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("connections", sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("connections", "deactivated_at")
    op.drop_column("data_sources", "deactivated_at")

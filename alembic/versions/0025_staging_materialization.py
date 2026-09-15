"""Phase 4.12: materialized staging dataset

Revision ID: 0025_staging_materialization
Revises: 0024_correction_evidence_cols
Create Date: 2026-09-15

Purely additive. Adds the columns needed to track a physical, full-dataset
staging table alongside the existing affected-record audit layer
(staging_records), and creates the DataCraft-owned `staging_data` schema
those physical tables live in.

All eight new staging_runs columns are nullable (copied_row_count defaults
to 0), so every existing row reads back with destination_table IS NULL —
the single authoritative "not materialized" signal the API contract uses
(see StagingRunNotMaterializedError). No existing row is touched, no
backfill is required.

`staging_runs.status` and its CHECK constraint (ck_staging_runs_status) are
NOT modified by this migration — that column's meaning (the affected-record
audit-layer build outcome: NOT_STARTED/BUILDING/READY/FAILED) is unchanged,
and so is everything that reads it (PublishingService's READY eligibility
check, StagingService's in-progress conflict check). Materialization
progress lives entirely in the new materialization_phase/progress_percentage
columns, an orthogonal dimension with its own CHECK constraint.

`staging_data` is created here so the backend never depends on it being
manually pre-provisioned; the materialization task also defensively issues
CREATE SCHEMA IF NOT EXISTS at runtime before creating a run's table, in
case a target database was provisioned before this migration ran.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_staging_materialization"
down_revision: Union[str, Sequence[str], None] = "0024_correction_evidence_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PHASE_VALUES = (
    "QUEUED",
    "PREPARING_SCHEMA",
    "CREATING_TABLE",
    "COPYING_SOURCE",
    "APPLYING_CORRECTIONS",
    "VALIDATING",
    "FINALIZING",
    "READY",
    "FAILED",
    "CANCELLED",
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS staging_data")

    op.add_column("staging_runs", sa.Column("destination_schema", sa.Text(), nullable=True))
    op.add_column("staging_runs", sa.Column("destination_table", sa.Text(), nullable=True))
    op.add_column("staging_runs", sa.Column("source_row_count", sa.Integer(), nullable=True))
    op.add_column("staging_runs", sa.Column("materialized_row_count", sa.Integer(), nullable=True))
    op.add_column("staging_runs", sa.Column("copied_row_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("staging_runs", sa.Column("materialization_phase", sa.Text(), nullable=True))
    op.add_column("staging_runs", sa.Column("progress_percentage", sa.SmallInteger(), nullable=True))
    op.add_column("staging_runs", sa.Column("materialization_error", sa.Text(), nullable=True))

    op.create_check_constraint(
        "ck_staging_runs_materialization_phase",
        "staging_runs",
        "materialization_phase IN (" + ", ".join(f"'{v}'" for v in _PHASE_VALUES) + ")",
    )
    op.create_check_constraint(
        "ck_staging_runs_progress_percentage",
        "staging_runs",
        "progress_percentage IS NULL OR (progress_percentage >= 0 AND progress_percentage <= 100)",
    )
    op.create_index(
        "idx_staging_runs_materialization_phase",
        "staging_runs",
        ["materialization_phase"],
        postgresql_where=sa.text("materialization_phase IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_staging_runs_materialization_phase", table_name="staging_runs")
    op.drop_constraint("ck_staging_runs_progress_percentage", "staging_runs", type_="check")
    op.drop_constraint("ck_staging_runs_materialization_phase", "staging_runs", type_="check")

    op.drop_column("staging_runs", "materialization_error")
    op.drop_column("staging_runs", "progress_percentage")
    op.drop_column("staging_runs", "materialization_phase")
    op.drop_column("staging_runs", "copied_row_count")
    op.drop_column("staging_runs", "materialized_row_count")
    op.drop_column("staging_runs", "source_row_count")
    op.drop_column("staging_runs", "destination_table")
    op.drop_column("staging_runs", "destination_schema")

    # staging_data is exclusively owned by this feature (no other migration
    # creates objects in it) — safe to drop cascade on downgrade so
    # upgrade/downgrade/upgrade round-trips cleanly on an isolated test DB.
    op.execute("DROP SCHEMA IF EXISTS staging_data CASCADE")

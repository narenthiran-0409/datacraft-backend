"""Jobs: add result column

Revision ID: 0019_jobs_result_column
Revises: 0018_data_preview_permission
Create Date: 2026-09-10

BUG FIX, found wiring the frontend to the AI Orchestrator's 4 async
suggestion-generation endpoints (POST /ai/suggestions/{run-summary,
prioritization,cluster,corrections}). Each returns only a job_id (202); the
Celery task behind it already computes {"ai_suggestion_id": ...} (or
"ai_suggestion_ids" for corrections) and returns it as the task's own return
value — but that return value only lands in Celery's internal result
backend, keyed by Celery's own internally-generated task id, which is never
exposed anywhere. jobs.job_id (the id actually returned to the client) is a
separate id entirely. The consequence: for 3 of the 5 AI suggestion types
(run_summary, prioritization, cluster — correction bypasses this by also
inserting directly into the pre-existing correction_suggestions table,
and explanation is fully synchronous), there was no way for any client to
ever discover which ai_suggestions row a completed job produced. Not a
migration compatibility break — a single nullable JSONB column, additive
only, mirroring every other "additive-only" touch point in this project's
history (e.g. migration 0016's own schema comments).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision: str = "0019_jobs_result_column"
down_revision: Union[str, Sequence[str], None] = "0018_data_preview_permission"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("result", pg.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "result")

"""Validation runs: add rules_evaluated_count and no_applicable_rules

Revision ID: 0022_validation_rule_counts
Revises: 0021_pattern_detected_origin
Create Date: 2026-09-12

Additive only: two NOT NULL columns on validation_runs, both server-
defaulted so existing rows backfill cleanly.

Why this migration exists: the validation worker (app/modules/validation/
tasks.py) resolves enabled RuleAssignments for a dataset and evaluates
only those. When a dataset has zero enabled assignments, the resolved
assignment list is empty, no evaluator ever runs, every row trivially
gets zero failures, and quality_score comes out 100.00 — indistinguishable
on the wire from "N rules evaluated, all rows genuinely passed." That
ambiguity is the root cause of the platform being able to report "100%
Passed" when nothing was actually checked.

rules_evaluated_count records the exact number of enabled RuleAssignments
the worker resolved and looped over for this run (not the number of Rule
rows that exist, not the number of ACTIVE rules — only what was actually
resolved against this dataset at execution time). no_applicable_rules is
a derived convenience flag (rules_evaluated_count == 0) so API consumers
don't have to re-derive it themselves.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_validation_rule_counts"
down_revision: Union[str, Sequence[str], None] = "0021_pattern_detected_origin"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "validation_runs",
        sa.Column("rules_evaluated_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "validation_runs",
        sa.Column("no_applicable_rules", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("validation_runs", "no_applicable_rules")
    op.drop_column("validation_runs", "rules_evaluated_count")

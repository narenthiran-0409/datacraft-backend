"""Correction suggestions: add strategy + evidence_detail (Phase 4.1 foundation)

Revision ID: 0024_correction_evidence_cols
Revises: 0023_correction_category
Create Date: 2026-09-13

Purely additive, both columns nullable, no backfill required and no
existing row is touched by this migration — every current row simply gets
NULL for both new columns, which is the correct "not yet computed by the
old code path" representation.

strategy: the name of the evidence/candidate-generation strategy that
produced this suggestion's value (e.g. "RATIO_CONSISTENCY",
"CONSTANT_WITHIN_GROUP", and — starting in later Phase 4 sub-phases —
"SEQUENCE_GAP", "STRING_TEMPLATE", "DATE_PROGRESSION"). NULL for every
row created by this phase's code (nothing writes it yet) and for any
existing DETERMINISTIC/pre-Phase-4 row, which has no such concept.

evidence_detail: the structured, aggregate evidence backing this specific
suggestion (candidate set, confidence, supporting/contradicting counts,
strategies attempted vs. rejected) — the durable, queryable counterpart to
`ai_suggestions.content->>'relationship_evidence'`, which today only lives
on the AISuggestion row and is reconstructed ad hoc. Kept as JSONB (not a
new table) for the same reason `correction_suggestions` already has no
side table for its other AI-shaped fields: this is still an evolving
shape being designed across Phase 4's sub-phases, and a JSONB column is
the append-friendly choice until/unless a query need justifies a real
table (see Phase 4A's plan — deliberately deferred).

No column here is populated, read, or otherwise used by any application
code yet — see app/modules/ai/candidates.py (Phase 4.1, pure/unwired) and
the Phase 4A investigation report for the wiring that will follow in a
later sub-phase, once explicitly approved.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024_correction_evidence_cols"
down_revision: Union[str, Sequence[str], None] = "0023_correction_category"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("correction_suggestions", sa.Column("strategy", sa.Text(), nullable=True))
    op.add_column(
        "correction_suggestions", sa.Column("evidence_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("correction_suggestions", "evidence_detail")
    op.drop_column("correction_suggestions", "strategy")

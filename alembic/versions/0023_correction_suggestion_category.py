"""Correction suggestions: add explicit category (confidence tier)

Revision ID: 0023_correction_category
Revises: 0022_validation_rule_counts
Create Date: 2026-09-13

Additive with backfill. correction_suggestions previously had no way to
distinguish "this is a deterministic, formula-based fix" from "the AI
looked at this and is not confident / could not infer a safe value" — every
row looked the same on the wire (a suggested_value string + a confidence
Decimal), and the AI path in particular stamped every single response with
the same flat placeholder confidence=0.500 regardless of what the model
actually said. That made it impossible for the UI (or a reviewer) to tell
"a real proposed value" apart from "AI explicitly declined to guess" —
exactly the ambiguity that let low-value/no-value suggestions look no
different from confident ones.

category is a small closed enum: DETERMINISTIC (the existing rule-based
generators — mode_fill/median_fill/range_clamp/trim_whitespace — always
produce this), AI_HIGH_CONFIDENCE (the AI path when it proposes a concrete
value it's actually confident in), NEEDS_REVIEW (AI has partial signal but
declines to guess — suggested_value is "" in this case, never a fabricated
value), CANNOT_INFER (AI has no usable signal at all, or the call/response
itself failed — suggested_value is also "").

Backfill: every existing row's source tells us enough to backfill safely.
source='RULE_BASED' rows are always DETERMINISTIC (that's the only kind
the deterministic generator registry ever produces). source='AI' rows
predate this column's existence and were always treated as a usable
proposed value by the old code path (no rejection based on confidence
tier existed), so they backfill to AI_HIGH_CONFIDENCE to preserve their
prior effective behavior — not a claim about their actual original
confidence, which was never captured.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_correction_category"
down_revision: Union[str, Sequence[str], None] = "0022_validation_rule_counts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CATEGORIES = ("DETERMINISTIC", "AI_HIGH_CONFIDENCE", "NEEDS_REVIEW", "CANNOT_INFER")


def upgrade() -> None:
    op.add_column("correction_suggestions", sa.Column("category", sa.Text(), nullable=True))
    op.execute(
        "UPDATE correction_suggestions SET category = "
        "CASE WHEN source = 'RULE_BASED' THEN 'DETERMINISTIC' ELSE 'AI_HIGH_CONFIDENCE' END"
    )
    op.alter_column("correction_suggestions", "category", nullable=False)
    op.create_check_constraint(
        "ck_correction_suggestions_category",
        "correction_suggestions",
        "category IN ('" + "','".join(_CATEGORIES) + "')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_correction_suggestions_category", "correction_suggestions", type_="check")
    op.drop_column("correction_suggestions", "category")

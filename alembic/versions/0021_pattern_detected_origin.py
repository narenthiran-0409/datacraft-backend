"""Rules: add PATTERN_DETECTED as an allowed rules.origin value

Revision ID: 0021_pattern_detected_origin
Revises: 0020_deactivated_at_column
Create Date: 2026-09-11

Additive only: widens ck_rules_origin from ('BUILT_IN', 'CUSTOM',
'AI_RECOMMENDED') to also allow 'PATTERN_DETECTED'. No existing row's
origin value stops satisfying the constraint.

Why a new value rather than reusing 'AI_RECOMMENDED': the whole-dataset
rule-detection feature has two halves — a deterministic pattern-matching
detector (column name + value-pattern + stat-ratio heuristics, no LLM
call) and an LLM-backed fallback for columns the heuristics aren't
confident about. Both halves land their output as rules.status=
'PENDING_REVIEW' rows (never active without an explicit human promote),
but only the second half is actually AI-generated. Labeling the first
half's output 'AI_RECOMMENDED' would misrepresent to a reviewer how a
given candidate rule was produced — a reviewer deciding whether to trust
a recommendation reasonably treats "an LLM proposed this" and "a regex
matched the column name and 92% of sampled values" as different kinds of
evidence. 'PATTERN_DETECTED' keeps that distinction honest. RulesService.
create_rule() (application layer) is what actually enforces that either
value forces status='PENDING_REVIEW' regardless of what's requested —
this migration only makes 'PATTERN_DETECTED' a legal value to store.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0021_pattern_detected_origin"
down_revision: Union[str, Sequence[str], None] = "0020_deactivated_at_column"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ORIGINAL_CHECK = "origin IN ('BUILT_IN', 'CUSTOM', 'AI_RECOMMENDED')"
_AMENDED_CHECK = "origin IN ('BUILT_IN', 'CUSTOM', 'AI_RECOMMENDED', 'PATTERN_DETECTED')"


def upgrade() -> None:
    op.drop_constraint("ck_rules_origin", "rules", type_="check")
    op.create_check_constraint("ck_rules_origin", "rules", _AMENDED_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_rules_origin", "rules", type_="check")
    op.create_check_constraint("ck_rules_origin", "rules", _ORIGINAL_CHECK)

"""Phase 11: Reports RBAC only (no new tables, no new indexes)

Revision ID: 0015_phase11_reports_rbac
Revises: 0014_phase10_lineage_foundation
Create Date: 2026-08-27

Reports is a strictly read-only aggregation module: it owns no tables and
requires no schema change beyond a single permission. Seeds permission
code reports.read (category: reports), linked UNIFORMLY to all five roles
(analyst, reviewer, approver, publisher, administrator) — unlike Phase
7/8/9's non-uniform splits, there is no reports.manage/create/export/admin
to withhold from any role, since none exists.

Index inspection (Phase 11 mandatory inspection item 7, Decision 4): the
existing indexes already cover every one of the five approved report
query patterns —
  idx_validation_runs_dataset_id_created_at: quality-trend, rule-
    effectiveness (both filter validation_runs by dataset_id + created_at
    range), and quality-by-dataset's per-dataset "latest run" window query.
  idx_validation_failures_rule_assignment_id: rule-effectiveness's join
    from validation_failures to rule_assignments.
  idx_issues_review_run_status / uq_corrections_issue_id: review-
    performance's issue-to-correction join.
  idx_approval_requests_status: approval-metrics' status grouping.
No new index is created — inspection did not demonstrate a genuine
insufficiency in existing coverage for any of the five patterns (Decision
4 requires actual demonstrated need, not speculative addition).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import String
from sqlalchemy import column, table

from alembic import op

revision: str = "0015_phase11_reports_rbac"
down_revision: Union[str, Sequence[str], None] = "0014_phase10_lineage_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ROLES_RECEIVING_REPORTS_READ = ["administrator", "analyst", "reviewer", "approver", "publisher"]


def upgrade() -> None:
    connection = op.get_bind()

    permissions_table = table("permissions", column("code", String), column("category", String))
    read_permission_id = connection.execute(
        permissions_table.insert().values(code="reports.read", category="reports").returning(column("id"))
    ).scalar_one()

    role_ids = {
        name: connection.execute(sa.text("SELECT id FROM roles WHERE name = :name"), {"name": name}).scalar_one()
        for name in ROLES_RECEIVING_REPORTS_READ
    }

    role_permissions_table = table("role_permissions", column("role_id"), column("permission_id"))
    op.bulk_insert(
        role_permissions_table,
        [{"role_id": role_ids[name], "permission_id": read_permission_id} for name in ROLES_RECEIVING_REPORTS_READ],
    )


def downgrade() -> None:
    connection = op.get_bind()

    connection.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = 'reports.read')"
        )
    )
    connection.execute(sa.text("DELETE FROM permissions WHERE code = 'reports.read'"))

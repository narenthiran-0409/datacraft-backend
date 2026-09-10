"""Data Preview: seed data_preview.read

Revision ID: 0018_data_preview_permission
Revises: 0017_phase5_rules_rbac_fix
Create Date: 2026-09-09

Data-only, additive-only migration. Seeds the permission code the new
GET /api/v1/datasets/{id}/preview endpoint is gated behind.

PERMISSION DECISION (see the backend task report for full reasoning):
this endpoint returns raw rows pulled live from the actual source
database — meaningfully more sensitive than anything metadata.read has
ever gated (column names/types/counts only, never row content). Reusing
metadata.read would silently widen that permission's real-world meaning
without anyone deciding to. A dedicated code makes the access-control
surface explicit and reviewable on its own, the same reasoning migration
0017 applied to rules.manage.

Role assignment: granted to all five roles (administrator, analyst,
reviewer, approver, publisher) uniformly, NOT restricted to a subset.
This mirrors every other pure-READ permission this project has ever
seeded (metadata.read, discovery.run, profiling.run, validation.run,
review.read, approval.read, staging.read, publish.read, lineage.read,
reports.read — all universal since migration 0007's docstring first
established the "every role inherits read access" convention). Every
non-uniform split in this project's history (approval.decide,
staging.create, publish.execute, rules.manage) has gated a WRITE/ACTION
with real consequences, never a read. data_preview.read is a pure read
with no side effects, so it follows the read-tier convention rather than
the write-tier one — but because this is the first permission gating
actual raw source content rather than metadata, this default is called
out explicitly here for deliberate review rather than assumed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import String
from sqlalchemy import column, table

from alembic import op

revision: str = "0018_data_preview_permission"
down_revision: Union[str, Sequence[str], None] = "0017_phase5_rules_rbac_fix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PERMISSION_CODE = "data_preview.read"
PERMISSION_CATEGORY = "preview"

ROLES_RECEIVING_PERMISSION = ["administrator", "analyst", "reviewer", "approver", "publisher"]


def upgrade() -> None:
    connection = op.get_bind()

    permissions_table = table("permissions", column("code", String), column("category", String))
    permission_id = connection.execute(
        permissions_table.insert().values(code=PERMISSION_CODE, category=PERMISSION_CATEGORY).returning(column("id"))
    ).scalar_one()

    role_ids = {
        name: connection.execute(sa.text("SELECT id FROM roles WHERE name = :name"), {"name": name}).scalar_one()
        for name in ROLES_RECEIVING_PERMISSION
    }

    role_permissions_table = table("role_permissions", column("role_id"), column("permission_id"))
    op.bulk_insert(
        role_permissions_table,
        [{"role_id": role_id, "permission_id": permission_id} for role_id in role_ids.values()],
    )


def downgrade() -> None:
    connection = op.get_bind()

    connection.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code = :code)"
        ),
        {"code": PERMISSION_CODE},
    )
    connection.execute(sa.text("DELETE FROM permissions WHERE code = :code"), {"code": PERMISSION_CODE})

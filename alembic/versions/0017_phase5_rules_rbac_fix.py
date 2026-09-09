"""Phase 5 correction: seed the Rule Catalog's actual permission codes

Revision ID: 0017_phase5_rules_rbac_fix
Revises: 0016_phase12_ai_foundation
Create Date: 2026-09-08

Note on the revision id: "0017_phase5_rules_rbac_correction" (the name the
brief this migration was written from actually specified) is 33 characters
— one over the 32-char limit of alembic_version.version_num (VARCHAR(32)).
Same issue, same fix as migration 0009's docstring already documents:
shortened to "...rbac_fix" to fit, discovered the same way (the upgrade
failed at the final `UPDATE alembic_version` step; the DDL/DML above it had
already succeeded in the same transaction and rolled back cleanly with it
since this project runs each `alembic upgrade` as one transaction).

Data-only, additive-only migration. Corrects a real gap between the locked
Phase 5 design and what migrations 0006/0007/0009 actually seeded: the Rule
Catalog module was designed around three permission codes —

  rules.read              broad, all five roles
  rules.manage             ADMINISTRATOR ONLY — rule creation/versioning is
                            gated more strictly than any other metadata-
                            management action in this project because
                            CUSTOM_EXPRESSION rules are code-execution-
                            adjacent
  rule_assignments.manage  analyst+ — assigning an already-approved rule to
                            a dataset is an operational action, not a
                            governance one

— but none of the three were ever seeded. app/api/v1/rules/routes.py has
been gating every rule action, including rule creation, behind
metadata.read/metadata.manage (Phase 3's discovery/metadata permissions,
granted to every role since migration 0007) instead. That means any
analyst has been able to create/version a rule, contradicting the specific
reasoning behind the admin-only gate. This migration only adds the three
missing codes and links them; it does not remove or alter metadata.read/
metadata.manage or any other existing permission — those remain in place
for the metadata/discovery module they actually belong to.

Role links, matching the reasoning above:

    Permission code            administrator  analyst  reviewer  approver  publisher
    rules.read                       x           x         x         x         x
    rules.manage                     x
    rule_assignments.manage          x           x         x         x         x
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy import String
from sqlalchemy import column, table

from alembic import op

revision: str = "0017_phase5_rules_rbac_fix"
down_revision: Union[str, Sequence[str], None] = "0016_phase12_ai_foundation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PERMISSIONS = [
    ("rules.read", "rules"),
    ("rules.manage", "rules"),
    ("rule_assignments.manage", "rules"),
]

ALL_ROLES = ["administrator", "analyst", "reviewer", "approver", "publisher"]

ROLE_PERMISSION_MATRIX = {
    "administrator": ["rules.read", "rules.manage", "rule_assignments.manage"],
    "analyst": ["rules.read", "rule_assignments.manage"],
    "reviewer": ["rules.read", "rule_assignments.manage"],
    "approver": ["rules.read", "rule_assignments.manage"],
    "publisher": ["rules.read", "rule_assignments.manage"],
}


def upgrade() -> None:
    connection = op.get_bind()

    permissions_table = table("permissions", column("code", String), column("category", String))
    permission_ids: dict[str, str] = {}
    for code, category in PERMISSIONS:
        permission_ids[code] = connection.execute(
            permissions_table.insert().values(code=code, category=category).returning(column("id"))
        ).scalar_one()

    role_ids = {
        name: connection.execute(sa.text("SELECT id FROM roles WHERE name = :name"), {"name": name}).scalar_one()
        for name in ALL_ROLES
    }

    role_permissions_table = table("role_permissions", column("role_id"), column("permission_id"))
    rows = [
        {"role_id": role_ids[role_name], "permission_id": permission_ids[code]}
        for role_name, codes in ROLE_PERMISSION_MATRIX.items()
        for code in codes
    ]
    op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    connection = op.get_bind()

    permission_codes = tuple(code for code, _ in PERMISSIONS)
    connection.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE code IN :codes)"
        ).bindparams(sa.bindparam("codes", expanding=True)),
        {"codes": permission_codes},
    )
    connection.execute(
        sa.text("DELETE FROM permissions WHERE code IN :codes").bindparams(
            sa.bindparam("codes", expanding=True)
        ),
        {"codes": permission_codes},
    )

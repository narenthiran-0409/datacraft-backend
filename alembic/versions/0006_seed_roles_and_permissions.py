"""seed roles and permissions

Revision ID: 0006_seed_roles_and_permissions
Revises: 0005_seed_connection_types
Create Date: 2026-08-19

Data-only migration. Seeds the 5 roles and 7 permission codes that Phase 2
actually needs, and links them per the following matrix:

    Permission code       administrator  analyst  reviewer  approver  publisher
    users.read                  x           x
    users.manage                x
    connections.read            x           x
    connections.manage          x
    data_sources.read           x           x
    data_sources.manage         x
    audit.read                  x           x

Rationale:
  - administrator gets all seven permission codes that exist in this phase
    (per the brief).
  - analyst gets exactly the four *.read permissions (per the brief) — a
    read-only operator who can see users, connections, data sources, and
    the audit trail, but cannot create/modify any of them.
  - reviewer, approver, publisher are seeded as identity rows now (their
    role names are part of the frozen, forward-looking role model — Review,
    Approval, and Staging/Publishing are later-phase modules per the Phase
    2 brief's explicit out-of-scope list) but are DELIBERATELY given zero
    role_permissions in this migration. None of the 7 permission codes that
    exist yet describe what a reviewer/approver/publisher actually does
    (there is no issues.review, approvals.decide, or publish_runs.execute
    permission yet, because those tables don't exist yet). Granting them
    Phase 2's identity/connections/data-source permissions instead would be
    an arbitrary, least-privilege-violating substitute for the permissions
    they should actually hold once their modules land — so they are left
    with none until the later phase that defines their real permission set
    also seeds it via its own migration.
"""
from typing import Sequence, Union

from sqlalchemy import table, column
from sqlalchemy import String, Boolean

from alembic import op

revision: str = "0006_seed_roles_and_permissions"
down_revision: Union[str, Sequence[str], None] = "0005_seed_connection_types"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_NAMES = ["analyst", "reviewer", "approver", "publisher", "administrator"]

PERMISSIONS = [
    ("users.read", "users"),
    ("users.manage", "users"),
    ("connections.read", "connections"),
    ("connections.manage", "connections"),
    ("data_sources.read", "data_sources"),
    ("data_sources.manage", "data_sources"),
    ("audit.read", "audit"),
]

ROLE_PERMISSION_MATRIX = {
    "administrator": [code for code, _ in PERMISSIONS],
    "analyst": ["users.read", "connections.read", "data_sources.read", "audit.read"],
    "reviewer": [],
    "approver": [],
    "publisher": [],
}


def upgrade() -> None:
    connection = op.get_bind()

    role_ids: dict[str, str] = {}
    for name in ROLE_NAMES:
        is_system = name == "administrator"
        result = connection.execute(
            table("roles", column("name", String), column("is_system_role", Boolean))
            .insert()
            .values(name=name, is_system_role=is_system)
            .returning(column("id"))
        )
        role_ids[name] = result.scalar_one()

    permission_ids: dict[str, str] = {}
    for code, category in PERMISSIONS:
        result = connection.execute(
            table("permissions", column("code", String), column("category", String))
            .insert()
            .values(code=code, category=category)
            .returning(column("id"))
        )
        permission_ids[code] = result.scalar_one()

    role_permissions_table = table("role_permissions", column("role_id"), column("permission_id"))
    rows = [
        {"role_id": role_ids[role_name], "permission_id": permission_ids[code]}
        for role_name, codes in ROLE_PERMISSION_MATRIX.items()
        for code in codes
    ]
    if rows:
        op.bulk_insert(role_permissions_table, rows)


def downgrade() -> None:
    role_names = tuple(ROLE_NAMES)
    permission_codes = tuple(code for code, _ in PERMISSIONS)

    op.execute(
        "DELETE FROM role_permissions WHERE role_id IN "
        f"(SELECT id FROM roles WHERE name IN ({', '.join(repr(n) for n in role_names)}))"
    )
    op.execute(f"DELETE FROM permissions WHERE code IN ({', '.join(repr(c) for c in permission_codes)})")
    op.execute(f"DELETE FROM roles WHERE name IN ({', '.join(repr(n) for n in role_names)})")

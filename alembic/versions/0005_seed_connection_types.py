"""seed connection_types

Revision ID: 0005_seed_connection_types
Revises: 0004_create_audit_events_table
Create Date: 2026-08-19

Data-only migration. Seeds the five connection types Phase 2's source
adapter factory dispatches on (app/source_adapters/factory.py): POSTGRESQL
is fully implemented for test_connection; the other four are registered
stubs (NotImplementedError) per Phase 2 scope.
"""
from typing import Sequence, Union

from sqlalchemy import table, column
from sqlalchemy import String, Boolean

from alembic import op

revision: str = "0005_seed_connection_types"
down_revision: Union[str, Sequence[str], None] = "0004_create_audit_events_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

connection_types_table = table(
    "connection_types",
    column("code", String),
    column("display_name", String),
    column("driver_module", String),
    column("is_active", Boolean),
)

SEED_ROWS = [
    {"code": "POSTGRESQL", "display_name": "PostgreSQL", "driver_module": "app.source_adapters.postgresql_provider", "is_active": True},
    {"code": "SQL_SERVER", "display_name": "SQL Server", "driver_module": "app.source_adapters.sqlserver_provider", "is_active": True},
    {"code": "MYSQL", "display_name": "MySQL", "driver_module": "app.source_adapters.mysql_provider", "is_active": True},
    {"code": "ORACLE", "display_name": "Oracle", "driver_module": "app.source_adapters.oracle_provider", "is_active": True},
    {"code": "SAP_HANA", "display_name": "SAP HANA", "driver_module": "app.source_adapters.saphana_provider", "is_active": True},
]


def upgrade() -> None:
    op.bulk_insert(connection_types_table, SEED_ROWS)


def downgrade() -> None:
    codes = tuple(row["code"] for row in SEED_ROWS)
    op.execute(
        f"DELETE FROM connection_types WHERE code IN ({', '.join(repr(c) for c in codes)})"
    )

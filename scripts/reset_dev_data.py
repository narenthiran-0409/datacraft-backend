"""Standalone script to wipe all real/user data from the dev database, while
preserving reference/config data and schema, ahead of a fresh human test pass.

Not run automatically by any migration or app startup path — run manually:

    python scripts/reset_dev_data.py

DEFAULT BEHAVIOR IS DRY RUN ONLY. With no flags, this script connects, prints
the resolved database (name + host), reports the current row count of every
table on the wipe list, and exits WITHOUT modifying anything.

To actually execute the wipe, TWO separate safeguards are required together:

    python scripts/reset_dev_data.py --confirm

    then, when prompted, type the literal database name ("dataquality") to
    proceed. Anything else aborts with no changes made.

After a real wipe, recreate an admin user and reseed AI prompts separately —
this script does not do either (see scripts/create_admin_user.py and
scripts/seed_ai_prompt_versions.py):

    python scripts/create_admin_user.py --email admin@example.com

WHAT IS WIPED vs PRESERVED

This project has a documented history of real dev-database data loss from
automated operations (see tests/conftest.py's top-of-file docstring), so this
script is deliberately conservative:

- WIPE_TABLES lists exactly the 36 user/run-data tables — verified against
  every __tablename__ in app/db/models/ (41 models total: 36 wiped + 5
  preserved below; alembic_version is a 42nd table, owned by Alembic, never
  a SQLAlchemy model, and never touched).
- Never touched, by name, regardless of what WIPE_TABLES contains: roles,
  permissions, role_permissions, connection_types (migration-seeded reference
  data from 0005/0006) and ai_prompt_versions (hand-authored prompt config —
  see scripts/seed_ai_prompt_versions.py's docstring for why this one is
  config, not user data), plus alembic_version (schema version bookkeeping).
  PRESERVE_TABLES exists only as a belt-and-braces cross-check: it asserts
  every model's tablename lands in exactly one of the two lists, so a table
  added to app/db/models/ in the future without a decision either way makes
  this script refuse to run instead of silently wiping or silently skipping it.

SAFETY

1. Default (no flags) is dry-run only — see above.
2. Real execution needs --confirm AND typing the database name at an
   interactive prompt.
3. Hard tripwire: refuses to run destructively unless settings.DATABASE_URL
   (the real app config, loaded the normal way) resolves to a database named
   exactly "dataquality" — never dataquality_test or anything else.
4. A single multi-table `TRUNCATE ... RESTART IDENTITY CASCADE`, mirroring
   tests/conftest.py's TRUNCATE_TABLES pattern, wrapped in one explicit
   transaction.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.base import Base

import app.db.models  # noqa: F401 — registers every model's table on Base.metadata

EXPECTED_DATABASE_NAME = "dataquality"

# Ordered so that dependent (child) tables are listed before the tables they
# reference — cosmetic only, since CASCADE resolves FK dependencies for us,
# but kept in the same order as tests/conftest.py's TRUNCATE_TABLES for
# readability and easy diffing against it.
WIPE_TABLES = [
    "ai_usage_logs",
    "ai_suggestions",
    "ai_messages",
    "ai_conversations",
    "audit_events",
    "lineage_records",
    "publish_runs",
    "staging_records",
    "staging_runs",
    "approval_decision_issues",
    "approval_decisions",
    "approval_requests",
    "corrections",
    "correction_suggestions",
    "issues",
    "review_runs",
    "validation_metrics",
    "validation_failures",
    "validation_results",
    "validation_runs",
    "rule_assignment_columns",
    "rule_assignments",
    "validation_templates",
    "rule_versions",
    "rules",
    "column_profiles",
    "profile_runs",
    "jobs",
    "dataset_key_columns",
    "columns",
    "datasets",
    "schemas",
    "connections",
    "data_sources",
    "user_roles",
    "users",
]

# Reference/config tables, and schema bookkeeping, that must survive a reset.
# alembic_version isn't a SQLAlchemy model (Alembic owns it directly), so it
# can't appear in Base.metadata.tables and is listed here for documentation
# only — _assert_wipe_list_matches_schema() doesn't need to check it.
PRESERVE_TABLES = [
    "roles",
    "permissions",
    "role_permissions",
    "connection_types",
    "ai_prompt_versions",
    "alembic_version",
]


def _assert_wipe_list_matches_schema() -> None:
    """Refuses to run if WIPE_TABLES/PRESERVE_TABLES have drifted from the
    actual model schema — e.g. a table added to app/db/models/ since this
    script was written, with no explicit decision made about it here."""
    model_tables = set(Base.metadata.tables.keys())
    declared_tables = set(WIPE_TABLES) | (set(PRESERVE_TABLES) - {"alembic_version"})

    missing_from_script = model_tables - declared_tables
    if missing_from_script:
        raise SystemExit(
            f"Refusing to run: {sorted(missing_from_script)} exist as models but are not "
            "classified in WIPE_TABLES or PRESERVE_TABLES in this script. Update the script "
            "with an explicit decision before running."
        )

    unknown_to_models = declared_tables - model_tables
    if unknown_to_models:
        raise SystemExit(
            f"Refusing to run: {sorted(unknown_to_models)} are listed in this script but do not "
            "exist as models. Update the script — it is out of sync with app/db/models/."
        )

    overlap = set(WIPE_TABLES) & set(PRESERVE_TABLES)
    if overlap:
        raise SystemExit(f"Refusing to run: {sorted(overlap)} are listed in both WIPE_TABLES and PRESERVE_TABLES.")


def _resolved_database() -> tuple[str, str]:
    url = make_url(settings.DATABASE_URL)
    return url.database or "", url.host or ""


def report_row_counts(engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in WIPE_TABLES:
            counts[table] = conn.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
    return counts


def print_report(db_name: str, db_host: str, counts: dict[str, int]) -> None:
    print(f"Target database: {db_name!r} on host {db_host!r}")
    print()
    print(f"{'table':<32} {'row_count':>10}")
    print("-" * 43)
    total = 0
    for table, count in counts.items():
        print(f"{table:<32} {count:>10}")
        total += count
    print("-" * 43)
    print(f"{'TOTAL':<32} {total:>10}")
    print()
    print("Preserved (never touched): " + ", ".join(PRESERVE_TABLES))


def execute_wipe(engine) -> None:
    table_list = ", ".join(f'"{t}"' for t in WIPE_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {table_list} RESTART IDENTITY CASCADE"))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Wipe all real/user data from the dev database (dry run by default).",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="Execute the wipe for real. Still requires typing the database name at an interactive prompt.",
    )
    args = parser.parse_args()

    _assert_wipe_list_matches_schema()

    db_name, db_host = _resolved_database()
    engine = create_engine(settings.DATABASE_URL)
    try:
        counts = report_row_counts(engine)
        print_report(db_name, db_host, counts)

        if not args.confirm:
            print()
            print("Dry run only - no changes made. Re-run with --confirm to execute the wipe.")
            return

        print()
        if db_name != EXPECTED_DATABASE_NAME:
            raise SystemExit(
                f"Refusing to run destructively: DATABASE_URL resolved to database {db_name!r}, "
                f"which is not {EXPECTED_DATABASE_NAME!r}. This script only ever wipes the real "
                "dev database it's explicitly written for, never anything else it happens to be "
                "pointed at."
            )

        print(f"About to TRUNCATE {len(WIPE_TABLES)} tables in database {db_name!r} on host {db_host!r}.")
        print("This cannot be undone.")
        typed = input(f"Type the database name ({db_name!r}) to proceed, or anything else to abort: ")
        if typed != db_name:
            print("Aborted - input did not match. No changes made.")
            return

        execute_wipe(engine)
        print(f"Done. Wiped {len(WIPE_TABLES)} tables in {db_name!r}.")
        print("Next: python scripts/create_admin_user.py --email <email>")
        print("      python scripts/seed_ai_prompt_versions.py  (only if ai_prompt_versions was also empty)")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

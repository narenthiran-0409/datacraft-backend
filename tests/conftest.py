"""IMPORTANT — test/dev database isolation.

Before this override existed, tests connected via the exact same
DATABASE_URL/engine/SessionLocal as the real app (app/core/database.py:
`engine = create_engine(settings.DATABASE_URL, ...)`), and the `db`
fixture below TRUNCATEs ~30 core tables (users, connections, datasets,
validation_runs, ...) at the start of every single test that uses it.
Pointed at the real dev database, that's not a hypothetical risk — it's
what happened: a real "dataquality" dev database was repeatedly wiped by
test runs sharing it (users table truncated mid-session, a deadlock from
an orphaned test connection, and eventually a full wipe down to just
migration-seeded reference data).

The fix has to happen here, before ANY `app.*` module is imported —
`app/core/config.py`'s `settings = get_settings()` is `@lru_cache`d and
evaluated at first import, and `app/core/database.py`'s `engine` is built
from `settings.DATABASE_URL` at ITS first import too. Once either has
been imported once in a process, later changing os.environ has no effect.
So: load .env the same way pydantic-settings would, then force
DATABASE_URL to a distinct `<original-db-name>_test` database — always,
unconditionally, regardless of what a developer's .env happens to say —
*before* the first `from app...` import below. This is what makes
isolation structural rather than a convention someone can forget: there
is no DATABASE_URL value a developer can put in .env that makes pytest
touch the real dev database, because pytest never reads that value for
its own connection — only to derive the *_test name from it.
"""
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_dev_database_url = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://dq_user:dq_password@localhost:5432/dataquality"
)
if not _dev_database_url.rsplit("/", 1)[-1].endswith("_test"):
    os.environ["DATABASE_URL"] = _dev_database_url.rsplit("/", 1)[0] + "/" + _dev_database_url.rsplit("/", 1)[-1] + "_test"

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.redis_client import get_redis_client
from app.core.security import create_access_token, hash_password
from app.db.models import Connection, ConnectionType, DataSource, Role, User, UserRole
from app.main import app
from app.modules.connections.credential_vault import LocalRedisVaultClient

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def _ensure_isolated_test_database() -> None:
    """Runs once per test session, before any test. Two jobs:

    1. Hard safety tripwire — refuses to run at all if, despite the
       override above, `settings.DATABASE_URL` doesn't resolve to a
       `*_test` database. This is the last line of defense against ever
       running tests (and their TRUNCATEs) against real dev data, in case
       the override above is ever edited carelessly in the future.
    2. Ensures the test database is migrated to head. Does NOT create the
       database itself — that requires CREATEDB or superuser privileges
       this project's normal app role (dq_user) intentionally doesn't
       have (see scripts/setup_test_postgres.sql, a one-time superuser-run
       step mirroring scripts/setup_local_postgres.sql). If it's missing,
       this raises a clear, actionable error rather than a confusing
       connection failure deep inside the first test.
    """
    test_db_url = make_url(settings.DATABASE_URL)
    if not test_db_url.database or not test_db_url.database.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run tests: DATABASE_URL resolved to '{test_db_url.database}', "
            "which does not end in '_test'. Tests must never run against a non-test "
            "database — see the top of tests/conftest.py."
        )

    maintenance_url = test_db_url.set(database="postgres")
    maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance_engine.connect() as conn:
            db_exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": test_db_url.database}
            ).scalar_one_or_none()
    finally:
        maintenance_engine.dispose()

    if db_exists is None:
        raise RuntimeError(
            f"Test database '{test_db_url.database}' does not exist. Provision it once "
            f"(as a Postgres superuser, same pattern as scripts/setup_local_postgres.sql):\n\n"
            f"    psql -U postgres -h {test_db_url.host} -f scripts/setup_test_postgres.sql\n\n"
            "This is a separate database from the real dev database and this step never "
            "touches it."
        )

    alembic_cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

TRUNCATE_TABLES = (
    "ai_usage_logs, ai_suggestions, ai_messages, ai_conversations, ai_prompt_versions, "
    "audit_events, lineage_records, publish_runs, staging_records, staging_runs, "
    "approval_decision_issues, approval_decisions, approval_requests, "
    "corrections, correction_suggestions, issues, review_runs, "
    "validation_metrics, validation_failures, validation_results, validation_runs, "
    "rule_assignment_columns, rule_assignments, validation_templates, rule_versions, rules, "
    "column_profiles, profile_runs, jobs, dataset_key_columns, columns, datasets, schemas, "
    "connections, data_sources, user_roles, users"
)


@pytest.fixture
def db() -> Session:
    session = SessionLocal()
    try:
        session.execute(text(f"TRUNCATE TABLE {TRUNCATE_TABLES} CASCADE"))
        session.commit()
        yield session
    finally:
        session.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def redis_client():
    r = get_redis_client()
    r.flushdb()
    yield r
    r.flushdb()


def _create_user_with_role(db: Session, role_name: str, email: str | None = None) -> User:
    role = db.execute(select(Role).where(Role.name == role_name)).scalar_one()
    user = User(
        email=email or f"{role_name}-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Str0ng!Passw0rd"),
        status="ACTIVE",
    )
    db.add(user)
    db.flush()
    db.add(UserRole(user_id=user.id, role_id=role.id))
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def admin_user(db: Session) -> User:
    return _create_user_with_role(db, "administrator", email="admin@example.com")


@pytest.fixture
def analyst_user(db: Session) -> User:
    return _create_user_with_role(db, "analyst", email="analyst@example.com")


@pytest.fixture
def no_role_user(db: Session) -> User:
    user = User(
        email=f"norole-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Str0ng!Passw0rd"),
        status="ACTIVE",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def auth_headers(user: User) -> dict:
    token, _ = create_access_token(user.id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(admin_user: User) -> dict:
    return auth_headers(admin_user)


@pytest.fixture
def analyst_headers(analyst_user: User) -> dict:
    return auth_headers(analyst_user)


@pytest.fixture
def no_role_headers(no_role_user: User) -> dict:
    return auth_headers(no_role_user)


@pytest.fixture
def pg_connection(db: Session, redis_client, admin_user: User) -> Connection:
    """A connection row pointing at this project's own local Postgres
    database — a real, reachable target for discovery/connection-test
    integration and e2e tests, without needing a second database."""
    db_url = urlparse(settings.DATABASE_URL.replace("postgresql+psycopg", "postgresql"))

    data_source = DataSource(name=f"Test DS {uuid.uuid4().hex[:8]}", created_by=admin_user.id)
    db.add(data_source)
    db.flush()

    pg_type = db.execute(select(ConnectionType).where(ConnectionType.code == "POSTGRESQL")).scalar_one()

    vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)
    credential_ref = vault.store({"username": db_url.username, "password": db_url.password})

    connection = Connection(
        data_source_id=data_source.id,
        connection_type_id=pg_type.id,
        name=f"Test Conn {uuid.uuid4().hex[:8]}",
        environment="DEV",
        host=db_url.hostname,
        port=db_url.port or 5432,
        database_name=db_url.path.lstrip("/"),
        username=db_url.username,
        credential_ref=credential_ref,
        created_by=admin_user.id,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection

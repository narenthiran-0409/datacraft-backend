import uuid
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal, engine
from app.core.redis_client import get_redis_client
from app.core.security import create_access_token, hash_password
from app.db.models import Connection, ConnectionType, DataSource, Role, User, UserRole
from app.main import app
from app.modules.connections.credential_vault import LocalRedisVaultClient

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

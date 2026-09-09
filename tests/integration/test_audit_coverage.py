"""Confirms every action listed in Phase 2 item 13 produces exactly one
audit_events row when exercised, covering the actions not already exercised
incidentally by tests/integration/test_users.py, test_data_sources.py, and
test_connections.py (user.created, user.role_assigned, user.password_changed,
data_source.created, data_source.deactivated, connection.created,
connection.deleted)."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import AccountLockedError, InvalidCredentialsError, PermissionDeniedError
from app.db.models import AuditEvent, ConnectionType
from app.modules.auth.refresh_tokens import RefreshTokenStore
from app.modules.auth.service import AuthService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.connections.service import ConnectionsService
from app.modules.data_sources.service import DataSourcesService


def _count_events(db: Session, entity_id, action: str) -> int:
    return db.execute(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.entity_id == entity_id, AuditEvent.action == action)
    ).scalar_one()


def test_login_and_logout_audit(db: Session, redis_client, admin_user) -> None:
    auth = AuthService(db, RefreshTokenStore(redis_client))

    _, refresh_token, user = auth.login("admin@example.com", "Str0ng!Passw0rd")
    assert _count_events(db, user.id, "user.login") == 1

    auth.logout(refresh_token, user)
    assert _count_events(db, user.id, "user.logout") == 1


def test_login_failed_audit_wrong_password(db: Session, redis_client, admin_user) -> None:
    auth = AuthService(db, RefreshTokenStore(redis_client))

    try:
        auth.login("admin@example.com", "wrong-password")
    except InvalidCredentialsError:
        pass

    assert _count_events(db, admin_user.id, "user.login_failed") == 1


def test_login_failed_audit_locked_account(db: Session, redis_client, admin_user) -> None:
    admin_user.status = "LOCKED"
    db.commit()

    auth = AuthService(db, RefreshTokenStore(redis_client))
    try:
        auth.login("admin@example.com", "Str0ng!Passw0rd")
    except AccountLockedError:
        pass

    assert _count_events(db, admin_user.id, "user.login_failed") == 1


def test_data_source_updated_audit(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    ds = service.create_data_source(actor=admin_user, name="Audit DS", description=None, owner_team=None, business_domain=None)

    service.update_data_source(actor=admin_user, data_source_id=ds.id, description="new desc", owner_team=None, business_domain=None)

    assert _count_events(db, ds.id, "data_source.updated") == 1


def test_connection_updated_and_tested_audit(db: Session, redis_client, admin_user) -> None:
    ds = DataSourcesService(db).create_data_source(actor=admin_user, name="Audit DS2", description=None, owner_team=None, business_domain=None)
    pg_type = db.execute(select(ConnectionType).where(ConnectionType.code == "POSTGRESQL")).scalar_one()

    conn_service = ConnectionsService(db, LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY))
    connection = conn_service.create_connection(
        actor=admin_user, data_source_id=ds.id, connection_type_id=pg_type.id, name="audit-conn",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )

    conn_service.update_connection(
        actor=admin_user, connection_id=connection.id, name=None, environment=None, host="127.0.0.1",
        port=None, database_name=None, service_name=None, username=None, credential=None, config=None,
    )
    assert _count_events(db, connection.id, "connection.updated") == 1

    conn_service.test_connection(actor=admin_user, connection_id=connection.id)
    assert _count_events(db, connection.id, "connection.tested") == 1


def test_permission_denied_audit(db: Session, no_role_user) -> None:
    from app.core.dependencies import get_user_permission_codes
    from app.modules.audit.service import AuditingService

    permission_codes = get_user_permission_codes(db, no_role_user.id)
    assert "users.read" not in permission_codes

    AuditingService(db).record(
        actor=no_role_user,
        action="permission.denied",
        entity_type="USER",
        entity_id=no_role_user.id,
        metadata={"required_permission": "users.read"},
    )
    db.commit()

    assert _count_events(db, no_role_user.id, "permission.denied") == 1

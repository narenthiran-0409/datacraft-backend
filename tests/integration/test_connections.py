import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import ConnectionNameAlreadyExistsError, DataSourceNotActiveError
from app.db.models import AuditEvent, ConnectionType
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.connections.service import ConnectionsService
from app.modules.data_sources.service import DataSourcesService


def _connections_service(db: Session, redis_client) -> ConnectionsService:
    return ConnectionsService(db, LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY))


def _pg_type(db: Session) -> ConnectionType:
    return db.execute(select(ConnectionType).where(ConnectionType.code == "POSTGRESQL")).scalar_one()


def test_create_connection_stores_only_credential_ref(db: Session, redis_client, admin_user) -> None:
    ds = DataSourcesService(db).create_data_source(
        actor=admin_user, name="DS1", description=None, owner_team=None, business_domain=None
    )
    service = _connections_service(db, redis_client)

    connection = service.create_connection(
        actor=admin_user,
        data_source_id=ds.id,
        connection_type_id=_pg_type(db).id,
        name="conn1",
        environment="DEV",
        host="localhost",
        port=5432,
        database_name="db1",
        service_name=None,
        username="svc",
        credential={"username": "svc", "password": "top-secret-password"},
        config={},
    )

    assert connection.credential_ref
    assert connection.credential_ref != "top-secret-password"

    raw_row = db.execute(
        text("SELECT credential_ref FROM connections WHERE id = :id"), {"id": str(connection.id)}
    ).scalar_one()
    assert raw_row == connection.credential_ref
    assert "top-secret-password" not in raw_row

    resolved = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY).resolve(connection.credential_ref)
    assert resolved["password"] == "top-secret-password"

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == connection.id, AuditEvent.action == "connection.created")
    ).scalars().all()
    assert len(events) == 1


def test_duplicate_connection_name_within_data_source_rejected(db: Session, redis_client, admin_user) -> None:
    ds = DataSourcesService(db).create_data_source(
        actor=admin_user, name="DS2", description=None, owner_team=None, business_domain=None
    )
    service = _connections_service(db, redis_client)
    pg_type_id = _pg_type(db).id

    service.create_connection(
        actor=admin_user, data_source_id=ds.id, connection_type_id=pg_type_id, name="dupe",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )

    with pytest.raises(ConnectionNameAlreadyExistsError):
        service.create_connection(
            actor=admin_user, data_source_id=ds.id, connection_type_id=pg_type_id, name="dupe",
            environment="DEV", host="localhost", port=5432, database_name="db2", service_name=None,
            username="svc", credential={"username": "svc", "password": "pw"}, config={},
        )


def test_same_connection_name_allowed_across_different_data_sources(db: Session, redis_client, admin_user) -> None:
    ds_service = DataSourcesService(db)
    ds1 = ds_service.create_data_source(actor=admin_user, name="DS3", description=None, owner_team=None, business_domain=None)
    ds2 = ds_service.create_data_source(actor=admin_user, name="DS4", description=None, owner_team=None, business_domain=None)
    service = _connections_service(db, redis_client)
    pg_type_id = _pg_type(db).id

    c1 = service.create_connection(
        actor=admin_user, data_source_id=ds1.id, connection_type_id=pg_type_id, name="same-name",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )
    c2 = service.create_connection(
        actor=admin_user, data_source_id=ds2.id, connection_type_id=pg_type_id, name="same-name",
        environment="DEV", host="localhost", port=5432, database_name="db2", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )
    assert c1.id != c2.id


def test_deactivate_connection_audits(db: Session, redis_client, admin_user) -> None:
    ds = DataSourcesService(db).create_data_source(actor=admin_user, name="DS5", description=None, owner_team=None, business_domain=None)
    service = _connections_service(db, redis_client)
    connection = service.create_connection(
        actor=admin_user, data_source_id=ds.id, connection_type_id=_pg_type(db).id, name="conn5",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )

    deactivated = service.deactivate_connection(actor=admin_user, connection_id=connection.id)
    assert deactivated.is_active is False

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == connection.id, AuditEvent.action == "connection.deleted")
    ).scalars().all()
    assert len(events) == 1


def test_reactivate_connection_audits(db: Session, redis_client, admin_user) -> None:
    ds = DataSourcesService(db).create_data_source(actor=admin_user, name="DS6", description=None, owner_team=None, business_domain=None)
    service = _connections_service(db, redis_client)
    connection = service.create_connection(
        actor=admin_user, data_source_id=ds.id, connection_type_id=_pg_type(db).id, name="conn6",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )
    service.deactivate_connection(actor=admin_user, connection_id=connection.id)

    reactivated = service.reactivate_connection(actor=admin_user, connection_id=connection.id)
    assert reactivated.is_active is True

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == connection.id, AuditEvent.action == "connection.reactivated")
    ).scalars().all()
    assert len(events) == 1


def test_reactivate_connection_rejected_when_data_source_inactive(db: Session, redis_client, admin_user) -> None:
    ds_service = DataSourcesService(db)
    ds = ds_service.create_data_source(actor=admin_user, name="DS7", description=None, owner_team=None, business_domain=None)
    service = _connections_service(db, redis_client)
    connection = service.create_connection(
        actor=admin_user, data_source_id=ds.id, connection_type_id=_pg_type(db).id, name="conn7",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None,
        username="svc", credential={"username": "svc", "password": "pw"}, config={},
    )
    service.deactivate_connection(actor=admin_user, connection_id=connection.id)
    ds_service.deactivate_data_source(actor=admin_user, data_source_id=ds.id)

    with pytest.raises(DataSourceNotActiveError):
        service.reactivate_connection(actor=admin_user, connection_id=connection.id)

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import DataSourceHasActiveConnectionsError
from app.db.models import AuditEvent, DataSource
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.connections.service import ConnectionsService
from app.modules.data_sources.service import DataSourcesService
from sqlalchemy import select


def test_create_data_source_audits(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    data_source = service.create_data_source(
        actor=admin_user, name="Billing DB", description=None, owner_team=None, business_domain=None,
    )

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == data_source.id, AuditEvent.action == "data_source.created")
    ).scalars().all()
    assert len(events) == 1


def test_deactivate_data_source_with_active_connection_rejected(db: Session, redis_client, admin_user) -> None:
    ds_service = DataSourcesService(db)
    data_source = ds_service.create_data_source(
        actor=admin_user, name="Orders DB", description=None, owner_team=None, business_domain=None,
    )

    from app.db.models import ConnectionType

    pg_type = db.execute(select(ConnectionType).where(ConnectionType.code == "POSTGRESQL")).scalar_one()

    conn_service = ConnectionsService(db, LocalRedisVaultClient(redis_client, "auxfxLith1IRMEbLccKJOaae4X4dQf7IITmLV3NpVS0="))
    conn_service.create_connection(
        actor=admin_user,
        data_source_id=data_source.id,
        connection_type_id=pg_type.id,
        name="primary",
        environment="DEV",
        host="localhost",
        port=5432,
        database_name="orders",
        service_name=None,
        username="svc",
        credential={"username": "svc", "password": "pw"},
        config={},
    )

    with pytest.raises(DataSourceHasActiveConnectionsError):
        ds_service.deactivate_data_source(actor=admin_user, data_source_id=data_source.id)


def test_deactivate_data_source_without_connections_succeeds(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    data_source = service.create_data_source(
        actor=admin_user, name="Unused DB", description=None, owner_team=None, business_domain=None,
    )

    deactivated = service.deactivate_data_source(actor=admin_user, data_source_id=data_source.id)
    assert deactivated.is_active is False

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == data_source.id, AuditEvent.action == "data_source.deactivated")
    ).scalars().all()
    assert len(events) == 1


def test_reactivate_data_source_audits(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    data_source = service.create_data_source(
        actor=admin_user, name="Reactivate Me DB", description=None, owner_team=None, business_domain=None,
    )
    service.deactivate_data_source(actor=admin_user, data_source_id=data_source.id)

    reactivated = service.reactivate_data_source(actor=admin_user, data_source_id=data_source.id)
    assert reactivated.is_active is True

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == data_source.id, AuditEvent.action == "data_source.reactivated")
    ).scalars().all()
    assert len(events) == 1


def test_deactivate_data_source_sets_deactivated_at(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    data_source = service.create_data_source(
        actor=admin_user, name="Timestamped DB", description=None, owner_team=None, business_domain=None,
    )
    assert data_source.deactivated_at is None

    deactivated = service.deactivate_data_source(actor=admin_user, data_source_id=data_source.id)
    assert deactivated.deactivated_at is not None


def test_reactivate_data_source_clears_deactivated_at(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    data_source = service.create_data_source(
        actor=admin_user, name="Reactivate Clears Timestamp DB", description=None, owner_team=None, business_domain=None,
    )
    service.deactivate_data_source(actor=admin_user, data_source_id=data_source.id)

    reactivated = service.reactivate_data_source(actor=admin_user, data_source_id=data_source.id)
    assert reactivated.deactivated_at is None


def _backdate_deactivation(db: Session, data_source: DataSource, days_ago: int) -> None:
    data_source.deactivated_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.commit()


def test_list_data_sources_excludes_inactive_older_than_visibility_window(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    stale = service.create_data_source(
        actor=admin_user, name="Stale Inactive DB", description=None, owner_team=None, business_domain=None,
    )
    service.deactivate_data_source(actor=admin_user, data_source_id=stale.id)
    _backdate_deactivation(db, stale, settings.INACTIVE_RECORD_VISIBILITY_DAYS + 1)

    assert stale.id not in {d.id for d in service.list_data_sources(is_active=False)}
    assert stale.id not in {d.id for d in service.list_data_sources()}
    # Still in the database, untouched — a query filter only, not a delete.
    assert db.get(DataSource, stale.id) is not None


def test_list_data_sources_includes_inactive_within_visibility_window(db: Session, admin_user) -> None:
    service = DataSourcesService(db)
    recent = service.create_data_source(
        actor=admin_user, name="Recently Inactive DB", description=None, owner_team=None, business_domain=None,
    )
    service.deactivate_data_source(actor=admin_user, data_source_id=recent.id)
    _backdate_deactivation(db, recent, settings.INACTIVE_RECORD_VISIBILITY_DAYS - 1)

    assert recent.id in {d.id for d in service.list_data_sources(is_active=False)}
    assert recent.id in {d.id for d in service.list_data_sources()}

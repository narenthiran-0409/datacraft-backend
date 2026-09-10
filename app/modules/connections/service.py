import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    ConnectionNameAlreadyExistsError,
    ConnectionNotFoundError,
    ConnectionTypeNotFoundError,
    DataSourceNotActiveError,
)
from app.db.models import Connection, ConnectionType, DataSource, User
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import CredentialVaultClient
from app.modules.lineage.service import LineageService
from app.source_adapters.exceptions import SourceAdapterError
from app.source_adapters.factory import get_provider


class ConnectionsService:
    def __init__(self, db: Session, vault: CredentialVaultClient) -> None:
        self._db = db
        self._vault = vault
        self._audit = AuditingService(db)
        self._lineage = LineageService(db)

    def list_connection_types(self) -> list[ConnectionType]:
        return list(self._db.execute(select(ConnectionType).order_by(ConnectionType.display_name)).scalars())

    def get_connection_type(self, connection_type_id: uuid.UUID) -> ConnectionType:
        connection_type = self._db.get(ConnectionType, connection_type_id)
        if connection_type is None:
            raise ConnectionTypeNotFoundError(f"Connection type {connection_type_id} not found")
        return connection_type

    def list_connections(self) -> list[Connection]:
        return list(self._db.execute(select(Connection).order_by(Connection.name)).scalars())

    def get_connection(self, connection_id: uuid.UUID) -> Connection:
        connection = self._db.get(Connection, connection_id)
        if connection is None:
            raise ConnectionNotFoundError(f"Connection {connection_id} not found")
        return connection

    def create_connection(
        self,
        *,
        actor: User,
        data_source_id: uuid.UUID,
        connection_type_id: uuid.UUID,
        name: str,
        environment: str,
        host: str,
        port: int,
        database_name: str | None,
        service_name: str | None,
        username: str,
        credential: dict,
        config: dict | None,
    ) -> Connection:
        self.get_connection_type(connection_type_id)

        existing = self._db.execute(
            select(Connection).where(Connection.data_source_id == data_source_id, func.lower(Connection.name) == name.lower())
        ).scalar_one_or_none()
        if existing is not None:
            raise ConnectionNameAlreadyExistsError(
                f"A connection named '{name}' already exists for this data source"
            )

        credential_ref = self._vault.store(credential)

        connection = Connection(
            data_source_id=data_source_id,
            connection_type_id=connection_type_id,
            name=name,
            environment=environment,
            host=host,
            port=port,
            database_name=database_name,
            service_name=service_name,
            username=username,
            credential_ref=credential_ref,
            config=config or {},
            created_by=actor.id,
        )
        self._db.add(connection)
        self._db.flush()

        # Phase 10 touch point 0 (additive-only): DATA_SOURCE -> CONNECTION,
        # written inside this same transaction, before commit.
        self._lineage.record_edge("DATA_SOURCE", data_source_id, "CONNECTION", connection.id, "DERIVED_FROM")

        self._audit.record(
            actor=actor,
            action="connection.created",
            entity_type="CONNECTION",
            entity_id=connection.id,
            after={"name": connection.name, "host": connection.host, "port": connection.port},
        )
        self._db.commit()
        self._db.refresh(connection)
        return connection

    def update_connection(
        self,
        *,
        actor: User,
        connection_id: uuid.UUID,
        name: str | None,
        environment: str | None,
        host: str | None,
        port: int | None,
        database_name: str | None,
        service_name: str | None,
        username: str | None,
        credential: dict | None,
        config: dict | None,
    ) -> Connection:
        connection = self.get_connection(connection_id)
        before = {"host": connection.host, "port": connection.port, "environment": connection.environment}

        if name is not None:
            connection.name = name
        if environment is not None:
            connection.environment = environment
        if host is not None:
            connection.host = host
        if port is not None:
            connection.port = port
        if database_name is not None:
            connection.database_name = database_name
        if service_name is not None:
            connection.service_name = service_name
        if username is not None:
            connection.username = username
        if config is not None:
            connection.config = config
        if credential is not None:
            connection.credential_ref = self._vault.store(credential)
        connection.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="connection.updated",
            entity_type="CONNECTION",
            entity_id=connection.id,
            before=before,
            after={"host": connection.host, "port": connection.port, "environment": connection.environment},
        )
        self._db.commit()
        self._db.refresh(connection)
        return connection

    def deactivate_connection(self, *, actor: User, connection_id: uuid.UUID) -> Connection:
        connection = self.get_connection(connection_id)
        connection.is_active = False
        connection.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="connection.deleted",
            entity_type="CONNECTION",
            entity_id=connection.id,
        )
        self._db.commit()
        self._db.refresh(connection)
        return connection

    def reactivate_connection(self, *, actor: User, connection_id: uuid.UUID) -> Connection:
        connection = self.get_connection(connection_id)

        # A connection's parent data source can itself be deactivated (data
        # sources only refuse deactivation while they still have active
        # connections — see DataSourcesService.deactivate_data_source's
        # active_connection_count guard). Reactivating a connection whose
        # data source is inactive would leave an active connection hanging
        # off an inactive data source, the exact inverted-parent state that
        # guard exists to prevent — so it's refused here instead.
        data_source = self._db.get(DataSource, connection.data_source_id)
        if not data_source.is_active:
            raise DataSourceNotActiveError(
                f"Cannot reactivate connection {connection_id}: parent data source "
                f"{connection.data_source_id} is not active; reactivate the data source first"
            )

        connection.is_active = True
        connection.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="connection.reactivated",
            entity_type="CONNECTION",
            entity_id=connection.id,
        )
        self._db.commit()
        self._db.refresh(connection)
        return connection

    def test_connection(self, *, actor: User, connection_id: uuid.UUID) -> Connection:
        connection = self.get_connection(connection_id)
        connection_type = self.get_connection_type(connection.connection_type_id)
        credential = self._vault.resolve(connection.credential_ref)

        provider = get_provider(
            connection_type.code,
            host=connection.host,
            port=connection.port,
            database=connection.database_name,
            username=credential.get("username", connection.username),
            password=credential.get("password", ""),
        )

        tested_at = datetime.now(timezone.utc)
        try:
            result = provider.test_connection()
            status_value = result.status
            latency_ms = result.latency_ms
            message = result.message
        except SourceAdapterError as exc:
            status_value = "OFFLINE"
            latency_ms = None
            message = str(exc)
        except NotImplementedError:
            status_value = "WARNING"
            latency_ms = None
            message = f"Connection testing is not yet implemented for {connection_type.code}"

        connection.status = status_value
        connection.last_tested_at = tested_at
        connection.last_test_latency_ms = latency_ms
        connection.updated_at = tested_at

        self._audit.record(
            actor=actor,
            action="connection.tested",
            entity_type="CONNECTION",
            entity_id=connection.id,
            after={"status": status_value, "latency_ms": latency_ms, "message": message},
        )
        self._db.commit()
        self._db.refresh(connection)
        return connection

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import (
    DataSourceHasActiveConnectionsError,
    DataSourceNameAlreadyExistsError,
    DataSourceNotFoundError,
)
from app.db.models import Connection, DataSource, User
from app.modules.audit.service import AuditingService


class DataSourcesService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)

    def list_data_sources(self) -> list[DataSource]:
        return list(self._db.execute(select(DataSource).order_by(DataSource.name)).scalars())

    def get_data_source(self, data_source_id: uuid.UUID) -> DataSource:
        data_source = self._db.get(DataSource, data_source_id)
        if data_source is None:
            raise DataSourceNotFoundError(f"Data source {data_source_id} not found")
        return data_source

    def create_data_source(
        self,
        *,
        actor: User,
        name: str,
        description: str | None,
        owner_team: str | None,
        business_domain: str | None,
    ) -> DataSource:
        existing = self._db.execute(select(DataSource).where(func.lower(DataSource.name) == name.lower())).scalar_one_or_none()
        if existing is not None:
            raise DataSourceNameAlreadyExistsError(f"A data source named '{name}' already exists")

        data_source = DataSource(
            name=name,
            description=description,
            owner_team=owner_team,
            business_domain=business_domain,
            created_by=actor.id,
        )
        self._db.add(data_source)
        self._db.flush()

        self._audit.record(
            actor=actor,
            action="data_source.created",
            entity_type="DATA_SOURCE",
            entity_id=data_source.id,
            after={"name": data_source.name},
        )
        self._db.commit()
        self._db.refresh(data_source)
        return data_source

    def update_data_source(
        self,
        *,
        actor: User,
        data_source_id: uuid.UUID,
        description: str | None,
        owner_team: str | None,
        business_domain: str | None,
    ) -> DataSource:
        data_source = self.get_data_source(data_source_id)
        before = {
            "description": data_source.description,
            "owner_team": data_source.owner_team,
            "business_domain": data_source.business_domain,
        }

        if description is not None:
            data_source.description = description
        if owner_team is not None:
            data_source.owner_team = owner_team
        if business_domain is not None:
            data_source.business_domain = business_domain
        data_source.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="data_source.updated",
            entity_type="DATA_SOURCE",
            entity_id=data_source.id,
            before=before,
            after={
                "description": data_source.description,
                "owner_team": data_source.owner_team,
                "business_domain": data_source.business_domain,
            },
        )
        self._db.commit()
        self._db.refresh(data_source)
        return data_source

    def deactivate_data_source(self, *, actor: User, data_source_id: uuid.UUID) -> DataSource:
        data_source = self.get_data_source(data_source_id)

        active_connection_count = self._db.execute(
            select(func.count())
            .select_from(Connection)
            .where(Connection.data_source_id == data_source_id, Connection.is_active.is_(True))
        ).scalar_one()
        if active_connection_count > 0:
            raise DataSourceHasActiveConnectionsError(
                f"Data source {data_source_id} has {active_connection_count} active connection(s); "
                "deactivate them first"
            )

        data_source.is_active = False
        data_source.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="data_source.deactivated",
            entity_type="DATA_SOURCE",
            entity_id=data_source.id,
        )
        self._db.commit()
        self._db.refresh(data_source)
        return data_source

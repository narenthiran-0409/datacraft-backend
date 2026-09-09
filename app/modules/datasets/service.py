import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import DatasetNotFoundError
from app.db.models import Column, Dataset, Schema, User
from app.modules.audit.service import AuditingService


class DatasetService:
    """Read paths only — no live source-database calls of any kind."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)

    def list_schemas(self, connection_id: uuid.UUID) -> list[Schema]:
        return list(
            self._db.execute(select(Schema).where(Schema.connection_id == connection_id).order_by(Schema.name)).scalars()
        )

    def list_datasets(
        self,
        *,
        schema_id: uuid.UUID | None,
        search: str | None,
        is_active: bool | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Dataset], int]:
        conditions = []
        if schema_id is not None:
            conditions.append(Dataset.schema_id == schema_id)
        if search:
            conditions.append(Dataset.name.ilike(f"%{search}%"))
        if is_active is not None:
            conditions.append(Dataset.is_active.is_(is_active))

        count_stmt = select(func.count()).select_from(Dataset)
        stmt = select(Dataset)
        for condition in conditions:
            count_stmt = count_stmt.where(condition)
            stmt = stmt.where(condition)

        total = self._db.execute(count_stmt).scalar_one()
        stmt = stmt.order_by(Dataset.name).offset((page - 1) * page_size).limit(page_size)
        items = list(self._db.execute(stmt).scalars())
        return items, total

    def get_dataset(self, dataset_id: uuid.UUID) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        return dataset

    def get_columns(self, dataset_id: uuid.UUID) -> list[Column]:
        self.get_dataset(dataset_id)
        return list(
            self._db.execute(
                select(Column).where(Column.dataset_id == dataset_id).order_by(Column.ordinal_position)
            ).scalars()
        )

    def set_active(self, *, actor: User, dataset_id: uuid.UUID, is_active: bool) -> Dataset:
        """Manual override of the auto-managed deactivation performed by Discovery."""
        dataset = self.get_dataset(dataset_id)
        before = {"is_active": dataset.is_active}

        dataset.is_active = is_active
        dataset.updated_at = datetime.now(timezone.utc)

        self._audit.record(
            actor=actor,
            action="dataset.updated",
            entity_type="DATASET",
            entity_id=dataset.id,
            before=before,
            after={"is_active": is_active},
        )
        self._db.commit()
        self._db.refresh(dataset)
        return dataset

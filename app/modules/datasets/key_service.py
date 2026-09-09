import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import DatasetNotFoundError, InvalidKeyColumnConfigurationError
from app.db.models import Column, Dataset, User
from app.modules.audit.service import AuditingService
from app.modules.datasets.key_resolution import apply_key_columns


class DatasetKeyService:
    """Manual key-column configuration for PK-less (or incorrectly-keyed)
    datasets. Reuses the same apply_key_columns() helper Discovery uses, so
    both paths stay in sync by construction."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)

    def set_key_columns(
        self, *, actor: User, dataset_id: uuid.UUID, key_columns: list[dict]
    ) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")

        if not key_columns:
            raise InvalidKeyColumnConfigurationError("At least one key column is required")

        ordinals = [kc["ordinal"] for kc in key_columns]
        if len(ordinals) != len(set(ordinals)):
            raise InvalidKeyColumnConfigurationError("Duplicate ordinals are not allowed")

        column_ids = [kc["column_id"] for kc in key_columns]
        if len(column_ids) != len(set(column_ids)):
            raise InvalidKeyColumnConfigurationError("Duplicate column_id values are not allowed")

        valid_column_ids = set(
            self._db.execute(
                select(Column.id).where(Column.dataset_id == dataset_id, Column.is_active.is_(True))
            ).scalars()
        )
        invalid = [str(cid) for cid in column_ids if cid not in valid_column_ids]
        if invalid:
            raise InvalidKeyColumnConfigurationError(
                f"Column(s) do not belong to this dataset (or are inactive): {', '.join(invalid)}"
            )

        ordered_column_ids = [kc["column_id"] for kc in sorted(key_columns, key=lambda kc: kc["ordinal"])]

        before = {"key_strategy": dataset.key_strategy}
        apply_key_columns(self._db, dataset, ordered_column_ids)

        self._audit.record(
            actor=actor,
            action="dataset.key_columns_configured",
            entity_type="DATASET",
            entity_id=dataset.id,
            before=before,
            after={"key_strategy": dataset.key_strategy, "column_ids": [str(c) for c in ordered_column_ids]},
        )
        self._db.commit()
        self._db.refresh(dataset)
        return dataset

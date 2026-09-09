"""Shared key-column resolution logic.

Used by BOTH app.modules.discovery.service.DiscoveryService (automatic
path, deriving the key set from SourceDatabaseProvider.get_primary_keys())
and app.modules.datasets.key_service.DatasetKeyService (manual path,
deriving the key set from a user's PUT request body). Deliberately factored
out so a future change to how a resolved key set is written applies
identically to both paths, rather than needing to be kept in sync by hand
in two places.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Column, Dataset, DatasetKeyColumn


def apply_key_columns(db: Session, dataset: Dataset, column_ids_in_order: list[uuid.UUID]) -> None:
    """Writes dataset_key_columns, columns.is_primary_key, and
    datasets.key_strategy for a resolved, non-empty, ordered key column
    set, all as part of the caller's current transaction (no commit here).

    column_ids_in_order must be non-empty — callers are responsible for
    deciding what "no key" means for their path (Discovery leaves existing
    configuration untouched when the source reports 0 PK columns; the
    manual path rejects an empty submission outright) before calling this.
    """
    if not column_ids_in_order:
        raise ValueError("apply_key_columns requires a non-empty column_ids_in_order")

    db.query(DatasetKeyColumn).filter(DatasetKeyColumn.dataset_id == dataset.id).delete()
    db.flush()

    for ordinal, column_id in enumerate(column_ids_in_order):
        db.add(DatasetKeyColumn(dataset_id=dataset.id, column_id=column_id, ordinal=ordinal))

    key_column_ids = set(column_ids_in_order)
    active_columns = db.execute(
        select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
    ).scalars().all()
    for col in active_columns:
        col.is_primary_key = col.id in key_column_ids

    dataset.key_strategy = "SINGLE_COLUMN" if len(column_ids_in_order) == 1 else "COMPOSITE"
    db.flush()

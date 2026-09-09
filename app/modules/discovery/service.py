import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Column, Dataset, Schema
from app.modules.datasets.key_resolution import apply_key_columns
from app.modules.lineage.service import LineageService


class DiscoveryService:
    """Upserts discovered metadata. Callers control the transaction
    boundary (this service flushes but never commits) — the Celery task in
    app.modules.discovery.tasks commits once per dataset, so a failure on
    one dataset can be rolled back and skipped without losing the whole run.

    Phase 10 touch point 1 (additive-only): CONNECTION -> SCHEMA and
    SCHEMA -> DATASET / DATASET -> COLUMN lineage edges are written here,
    inside these same per-schema/per-dataset units, riding the Celery
    task's existing commit — never a new, separate transaction.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._lineage = LineageService(db)

    def upsert_schema(self, connection_id: uuid.UUID, schema_name: str) -> Schema:
        now = datetime.now(timezone.utc)
        schema = self._db.execute(
            select(Schema).where(Schema.connection_id == connection_id, Schema.name == schema_name)
        ).scalar_one_or_none()

        if schema is None:
            schema = Schema(connection_id=connection_id, name=schema_name, is_active=True, discovered_at=now)
            self._db.add(schema)
        else:
            schema.is_active = True
            schema.discovered_at = now
            schema.updated_at = now

        self._db.flush()
        self._lineage.record_edge("CONNECTION", connection_id, "SCHEMA", schema.id, "DERIVED_FROM")
        return schema

    def upsert_dataset(
        self,
        schema: Schema,
        dataset_info: dict[str, Any],
        columns_info: list[dict[str, Any]],
        primary_key_names: list[str],
        row_count: int | None,
    ) -> Dataset:
        now = datetime.now(timezone.utc)

        dataset = self._db.execute(
            select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == dataset_info["name"])
        ).scalar_one_or_none()

        if dataset is None:
            dataset = Dataset(
                schema_id=schema.id,
                name=dataset_info["name"],
                object_type=dataset_info.get("object_type", "TABLE"),
                is_active=True,
                discovered_at=now,
            )
            self._db.add(dataset)
            self._db.flush()
        else:
            dataset.object_type = dataset_info.get("object_type", dataset.object_type)
            dataset.is_active = True
            dataset.discovered_at = now
            dataset.updated_at = now

        self._lineage.record_edge("SCHEMA", schema.id, "DATASET", dataset.id, "DERIVED_FROM")

        dataset.row_count_estimate = row_count

        # (b) upsert every column seen this run; deactivate this dataset's
        # columns that existed but were not seen this run.
        seen_names: set[str] = set()
        name_to_column: dict[str, Column] = {}
        column_lineage_edges: list[tuple[str, uuid.UUID, str, uuid.UUID, str]] = []
        for col_info in columns_info:
            name = col_info["name"]
            seen_names.add(name)

            column = self._db.execute(
                select(Column).where(Column.dataset_id == dataset.id, Column.name == name)
            ).scalar_one_or_none()
            is_new = column is None
            if is_new:
                column = Column(dataset_id=dataset.id, name=name)

            column.ordinal_position = col_info["ordinal_position"]
            column.native_data_type = col_info.get("native_data_type")
            column.normalized_data_type = col_info.get("normalized_data_type", "STRING")
            column.max_length = col_info.get("max_length")
            column.numeric_precision = col_info.get("numeric_precision")
            column.numeric_scale = col_info.get("numeric_scale")
            column.is_nullable = col_info.get("is_nullable", True)
            column.is_active = True
            column.discovered_at = now
            column.updated_at = now

            if is_new:
                self._db.add(column)
                self._db.flush()
            name_to_column[name] = column
            column_lineage_edges.append(("DATASET", dataset.id, "COLUMN", column.id, "DERIVED_FROM"))

        # One bulk insert for all columns reconciled this run, not a loop of
        # individual inserts — matching this project's established
        # single-query-for-all-rows performance discipline.
        self._lineage.record_edges_bulk(column_lineage_edges)

        previously_active_columns = self._db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
        ).scalars().all()
        for column in previously_active_columns:
            if column.name not in seen_names:
                column.is_active = False
                column.updated_at = now

        self._db.flush()

        # (c)/(d) resolve the key set. 0 columns -> leave dataset_key_columns
        # (and is_primary_key flags) entirely untouched, per spec.
        if primary_key_names:
            column_ids_in_order = [
                name_to_column[name].id for name in primary_key_names if name in name_to_column
            ]
            if column_ids_in_order:
                apply_key_columns(self._db, dataset, column_ids_in_order)

        # (e) column_count reflects the just-reconciled active column set.
        dataset.column_count = self._db.execute(
            select(func.count()).select_from(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
        ).scalar_one()

        self._db.flush()
        return dataset

    def deactivate_missing(
        self,
        connection_id: uuid.UUID,
        discovered_schema_names: set[str],
        discovered_dataset_keys: set[tuple[uuid.UUID, str]],
    ) -> None:
        now = datetime.now(timezone.utc)

        schemas = self._db.execute(
            select(Schema).where(Schema.connection_id == connection_id, Schema.is_active.is_(True))
        ).scalars().all()
        for schema in schemas:
            if schema.name not in discovered_schema_names:
                schema.is_active = False
                schema.updated_at = now

        datasets = self._db.execute(
            select(Dataset)
            .join(Schema, Schema.id == Dataset.schema_id)
            .where(Schema.connection_id == connection_id, Dataset.is_active.is_(True))
        ).scalars().all()
        for dataset in datasets:
            if (dataset.schema_id, dataset.name) not in discovered_dataset_keys:
                dataset.is_active = False
                dataset.updated_at = now

        self._db.flush()

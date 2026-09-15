"""Phase 4.6 — business-key discovery/confirmation orchestration.

Two entirely separate operations, deliberately kept apart:

- discover(): read-only. Fetches one bounded sample (via the existing
  SourceDatabaseProvider.sample_rows(), never a new provider capability)
  and runs it through the pure app.modules.datasets.business_key_discovery
  engine. NEVER writes dataset_key_columns or datasets.key_strategy.

- confirm(): the only path that may adopt a candidate. Always
  reverifies live against the current source data before adopting —
  never trusts evidence a caller is merely relaying back from an earlier
  discover() call. Only ever adopts when the fresh reverification is
  exactly VERIFIED_UNIQUE for exactly the columns the caller named.
  Reuses app.modules.datasets.key_resolution.apply_key_columns — the
  exact same function Discovery's automatic source-PK path and
  DatasetKeyService's manual path already share — so there is exactly
  one declared-key representation and exactly one downstream consumer
  chain (parse_record_ref_to_key_dict -> Phase 4.7 fetch_rows_by_keys).
  This module implements no second retrieval mechanism of its own.
"""
import uuid
from dataclasses import dataclass

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    DatasetNotActiveError,
    DatasetNotFoundError,
    InvalidKeyColumnConfigurationError,
)
from app.db.models import Column, Connection, ConnectionType, Dataset, Schema, User
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import CredentialVaultClient
from app.modules.datasets.business_key_discovery import (
    BusinessKeyDiscoveryResult,
    ColumnMeta,
    STATUS_VERIFIED_UNIQUE,
    discover_business_key_candidates,
)
from app.modules.datasets.key_resolution import apply_key_columns
from app.source_adapters.factory import get_provider

_RELIABLE_KEY_STRATEGIES = frozenset({"SINGLE_COLUMN", "COMPOSITE"})


@dataclass(frozen=True)
class BusinessKeyDiscoveryReport:
    dataset_id: uuid.UUID
    existing_key_strategy: str
    already_has_reliable_key: bool
    result: BusinessKeyDiscoveryResult | None


class BusinessKeyService:
    def __init__(self, db: Session, vault: CredentialVaultClient, redis_client: Redis) -> None:
        self._db = db
        self._vault = vault
        self._redis = redis_client
        self._audit = AuditingService(db)

    def discover(self, *, actor: User, dataset_id: uuid.UUID) -> BusinessKeyDiscoveryReport:
        dataset = self._get_active_dataset(dataset_id)

        if dataset.key_strategy in _RELIABLE_KEY_STRATEGIES:
            # Existing source PK / existing declared key is preferred —
            # detection does not run at all, so it can never suggest
            # replacing an already-reliable key.
            return BusinessKeyDiscoveryReport(
                dataset_id=dataset.id, existing_key_strategy=dataset.key_strategy,
                already_has_reliable_key=True, result=None,
            )

        result = self._run_discovery(dataset)

        self._audit.record(
            actor=actor, action="dataset.business_key_discovery_run", entity_type="DATASET", entity_id=dataset.id,
            metadata={
                "status": result.status,
                "recommended_columns": list(result.recommended.columns) if result.recommended else None,
                "total_rows_evaluated": result.total_rows_evaluated,
                "is_full_scan": result.is_full_scan,
                "widths_searched": list(result.widths_searched),
            },
        )
        self._db.commit()

        return BusinessKeyDiscoveryReport(
            dataset_id=dataset.id, existing_key_strategy=dataset.key_strategy,
            already_has_reliable_key=False, result=result,
        )

    def confirm(self, *, actor: User, dataset_id: uuid.UUID, columns: list[str]) -> Dataset:
        dataset = self._get_active_dataset(dataset_id)

        if dataset.key_strategy in _RELIABLE_KEY_STRATEGIES:
            raise InvalidKeyColumnConfigurationError(
                f"Dataset {dataset_id} already has a reliable key_strategy "
                f"({dataset.key_strategy}); business-key confirmation is only for "
                "datasets currently on ROW_INDEX_FALLBACK"
            )
        if not columns:
            raise InvalidKeyColumnConfigurationError("At least one column is required to confirm a business key")

        # Reverify live, right now — never trust evidence relayed back from
        # an earlier discover() call. If the source data changed between
        # discovery and this confirmation (new duplicate, new null, a
        # column dropped), that must surface here as a rejection, not a
        # silent adoption of a candidate that is no longer actually valid.
        result = self._run_discovery(dataset)

        if result.status != STATUS_VERIFIED_UNIQUE:
            raise InvalidKeyColumnConfigurationError(
                f"Candidate is not currently adoptable (status={result.status}); only a freshly "
                "reverified VERIFIED_UNIQUE candidate may be confirmed"
            )
        if result.recommended.columns != tuple(columns):
            raise InvalidKeyColumnConfigurationError(
                "Requested columns do not match the currently (re)verified candidate — the "
                f"underlying data may have changed since discovery (verified: "
                f"{result.recommended.columns}, requested: {tuple(columns)})"
            )

        active_columns = self._active_columns(dataset)
        name_to_id = {c.name: c.id for c in active_columns}
        missing = [name for name in columns if name not in name_to_id]
        if missing:
            raise InvalidKeyColumnConfigurationError(f"Unknown or inactive column(s): {', '.join(missing)}")
        column_ids_in_order = [name_to_id[name] for name in result.recommended.columns]

        before = {"key_strategy": dataset.key_strategy}
        apply_key_columns(self._db, dataset, column_ids_in_order)

        self._audit.record(
            actor=actor, action="dataset.business_key_confirmed", entity_type="DATASET", entity_id=dataset.id,
            before=before,
            after={"key_strategy": dataset.key_strategy, "column_ids": [str(c) for c in column_ids_in_order]},
            metadata={
                "columns": list(result.recommended.columns),
                "verification_level": result.recommended.verification_level,
                "total_rows_evaluated": result.recommended.total_rows_evaluated,
            },
        )
        self._db.commit()
        self._db.refresh(dataset)
        return dataset

    # --- internal helpers ---

    def _get_active_dataset(self, dataset_id: uuid.UUID) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        if not dataset.is_active:
            raise DatasetNotActiveError(f"Dataset {dataset_id} is not active")
        return dataset

    def _active_columns(self, dataset: Dataset) -> list[Column]:
        return list(
            self._db.execute(
                select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
            ).scalars()
        )

    def _run_discovery(self, dataset: Dataset) -> BusinessKeyDiscoveryResult:
        active_columns = self._active_columns(dataset)
        schema_row = self._db.get(Schema, dataset.schema_id)
        connection = self._db.get(Connection, schema_row.connection_id)
        connection_type = self._db.get(ConnectionType, connection.connection_type_id)

        provider = None
        try:
            credential = self._vault.resolve(connection.credential_ref)
            provider = get_provider(
                connection_type.code, host=connection.host, port=connection.port,
                database=connection.database_name, username=credential.get("username", connection.username),
                password=credential.get("password", ""),
            )
            sample = provider.sample_rows(
                schema_row.name, dataset.name, settings.BUSINESS_KEY_DISCOVERY_SAMPLE_SIZE,
                row_count_estimate=dataset.row_count_estimate,
            )
        finally:
            if provider is not None:
                provider.close()

        column_metas = [ColumnMeta(name=c.name, normalized_data_type=c.normalized_data_type) for c in active_columns]
        return discover_business_key_candidates(columns=column_metas, rows=sample.rows, is_full_scan=sample.is_full_scan)

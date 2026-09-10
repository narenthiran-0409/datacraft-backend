"""Live-source Data Preview. Deliberately NOT part of DatasetService (whose
own docstring is explicit: "Read paths only — no live source-database
calls of any kind") — this is the one dataset-scoped read that genuinely
needs a live connection to the actual source database, via the same
credential-vault + get_provider() resolution Discovery and Profiling
already use.

Row-count cap: PREVIEW_MAX_ROWS (100) is enforced here by clamping the
caller-requested row count before it ever reaches the provider — silently,
not via a 422 rejection. This is a deliberate departure from Profiling's
"never silently downgrade" philosophy (see ProfilingService._resolve_sample_size):
a profiling run's exact sample size affects the statistics it computes, so
silently shrinking it would be a correctness issue; a preview is a bounded
"peek" with no computed output whose accuracy depends on the exact count,
so capping it is a UX nicety, not a correctness compromise.

Truncation: string values are capped to PREVIEW_MAX_VALUE_LENGTH characters
before leaving this service, mirroring the precedent set by
app.modules.profiling.engine's MAX_TOP_VALUE_LENGTH (Phase 4's top-value
capture) — same 100-char threshold, same "don't return unbounded blob/text
content" reasoning, kept as an independent local constant rather than a
cross-module import since the two are conceptually unrelated (column-level
frequency aggregation vs. row-level preview display) and shouldn't drift
together by accident.
"""
import uuid
from typing import Any

from redis import Redis
from sqlalchemy.orm import Session

from app.core.exceptions import (
    CredentialVaultError,
    DatasetNotActiveError,
    DatasetNotFoundError,
    PreviewSourceUnavailableError,
    PreviewTimeoutError,
)
from app.db.models import Connection, ConnectionType, Dataset, Schema, User
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import CredentialVaultClient
from app.source_adapters.exceptions import (
    SourceAdapterError,
    SourceTimeoutError,
)
from app.source_adapters.factory import get_provider

PREVIEW_MAX_ROWS = 100
PREVIEW_DEFAULT_ROWS = 20
PREVIEW_MAX_VALUE_LENGTH = 100


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > PREVIEW_MAX_VALUE_LENGTH:
        return value[:PREVIEW_MAX_VALUE_LENGTH]
    return value


class PreviewService:
    def __init__(self, db: Session, vault: CredentialVaultClient, redis_client: Redis) -> None:
        self._db = db
        self._vault = vault
        self._redis = redis_client
        self._audit = AuditingService(db)

    def preview_dataset(self, *, actor: User, dataset_id: uuid.UUID, requested_row_count: int) -> dict:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        if not dataset.is_active:
            raise DatasetNotActiveError(f"Dataset {dataset_id} is not active")

        schema_row = self._db.get(Schema, dataset.schema_id)
        connection = self._db.get(Connection, schema_row.connection_id)
        connection_type = self._db.get(ConnectionType, connection.connection_type_id)

        row_count = min(requested_row_count, PREVIEW_MAX_ROWS)
        was_capped = requested_row_count > PREVIEW_MAX_ROWS

        provider = None
        try:
            try:
                credential = self._vault.resolve(connection.credential_ref)
                provider = get_provider(
                    connection_type.code,
                    host=connection.host,
                    port=connection.port,
                    database=connection.database_name,
                    username=credential.get("username", connection.username),
                    password=credential.get("password", ""),
                )
                sample = provider.sample_rows(schema_row.name, dataset.name, row_count)
            except SourceTimeoutError as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._audit_failure(actor, dataset, message)
                raise PreviewTimeoutError(message) from exc
            except (SourceAdapterError, CredentialVaultError) as exc:
                message = f"{type(exc).__name__}: {exc}"
                self._audit_failure(actor, dataset, message)
                raise PreviewSourceUnavailableError(message) from exc
        finally:
            if provider is not None:
                provider.close()

        rows = [{col: _truncate(val) for col, val in row.items()} for row in sample.rows]
        columns = list(rows[0].keys()) if rows else []

        self._audit.record(
            actor=actor,
            action="dataset.previewed",
            entity_type="DATASET",
            entity_id=dataset.id,
            metadata={
                "schema": schema_row.name,
                "table": dataset.name,
                "requested_row_count": requested_row_count,
                "returned_row_count": len(rows),
                "capped_to_max": was_capped,
            },
        )
        self._db.commit()

        return {
            "dataset_id": dataset.id,
            "schema_name": schema_row.name,
            "table_name": dataset.name,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "requested_row_count": requested_row_count,
            "capped_to_max": was_capped,
        }

    def _audit_failure(self, actor: User, dataset: Dataset, message: str) -> None:
        # Never include raw preview values here — only the fact that an
        # attempt was made and why it failed. See tests/integration
        # /test_staging.py::test_audit_events_contain_no_raw_values for this
        # project's standing policy on what audit metadata may hold.
        self._audit.record(
            actor=actor,
            action="dataset.preview_failed",
            entity_type="DATASET",
            entity_id=dataset.id,
            metadata={"error": message},
        )
        self._db.commit()

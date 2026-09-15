"""Phase 4.12 — materialized staging dataset. Async Celery counterpart to
StagingService's synchronous affected-record audit build: reads the WHOLE
source table (bounded-memory, batched, via provider.iter_rows()) into a
DataCraft-owned physical PostgreSQL table under the `staging_data` schema,
then overlays the same approved corrections already recorded in
staging_records (built synchronously by StagingService.trigger()) onto the
matching rows.

Follows the exact session/idempotency/fail/cancel/audit pattern established
by app.modules.validation.tasks.run_validation and
app.modules.publishing.tasks.run_publish: explicit SessionLocal() with a
try/finally close, an idempotency guard against redelivery, a catch-all
safety net so job/staging_run can never get stuck mid-phase forever, and
cooperative cancellation checks via JobsService.is_cancel_requested()
between batches.

Never writes to the SOURCE database — only ever reads from `provider`
(count_rows/iter_rows/fetch_rows_by_keys are the only provider calls made
here). Every write in this module targets staging_data.<table>, which lives
in DataCraft's own PostgreSQL (the same database/session as everything
else), via the ordinary `db` session — never through a source_adapters
provider.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import CredentialVaultError
from app.core.redis_client import get_redis_client
from app.db.models import Connection, ConnectionType, Dataset, Schema, StagingRecord, StagingRun, User
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.jobs.service import JobsService
from app.modules.staging.destination_naming import STAGING_SCHEMA_NAME, build_destination_table_name, quote_pg_identifier
from app.modules.staging.record_builder import parse_record_ref_to_key_dict
from app.modules.staging.service import StagingService
from app.modules.staging.type_mapping import normalized_type_to_pg_ddl
from app.modules.staging.value_coercion import CorrectionCoercionError, coerce_final_value
from app.source_adapters.exceptions import (
    SourceAuthenticationError,
    SourceQueryError,
    SourceSSLError,
    SourceTimeoutError,
    SourceUnreachableError,
)
from app.source_adapters.factory import get_provider

_TERMINAL_SOURCE_ERRORS = (
    SourceAuthenticationError,
    SourceTimeoutError,
    SourceUnreachableError,
    SourceSSLError,
    SourceQueryError,
    CredentialVaultError,
)

_TERMINAL_JOB_STATUSES = ("CANCELLED", "COMPLETED", "FAILED")
_TERMINAL_PHASES = ("READY", "FAILED", "CANCELLED")


class _MaterializationCancelled(Exception):
    pass


class RowCountMismatchError(Exception):
    """Raised when the exact row count taken before COPYING_SOURCE doesn't
    match the number of rows actually copied — the row-count invariant this
    phase must never silently violate. Caught by the task's outer handler
    and converted into a FAILED run, never a READY one."""


@celery_app.task(name="staging.run_staging_materialization")
def run_staging_materialization(job_id: str, staging_run_id: str) -> dict:
    db = SessionLocal()
    try:
        redis_client = get_redis_client()
        jobs_service = JobsService(db, redis_client)
        job = jobs_service.get(uuid.UUID(job_id))
        staging_run = db.get(StagingRun, uuid.UUID(staging_run_id))

        if (
            job.status in _TERMINAL_JOB_STATUSES
            or staging_run is None
            or staging_run.materialization_phase in _TERMINAL_PHASES
        ):
            # Idempotency guard — same rationale as run_validation/
            # run_publish: a redelivered Celery message, or a manual
            # re-invocation by job_id/staging_run_id alone, for a
            # materialization already resolved.
            return {"status": job.status}

        actor = db.get(User, job.created_by) if job.created_by else None
        audit = AuditingService(db)

        try:
            return _execute_materialization(db, redis_client, jobs_service, audit, job, staging_run, actor)
        except Exception as exc:
            # Catch-all safety net — mirrors run_validation's. Every error
            # this task anticipates (source errors, coercion failures, the
            # row-count invariant, cancellation) is already handled and
            # returned from inside _execute_materialization() itself.
            # Reaching here means something genuinely unexpected happened.
            db.rollback()
            message = f"{type(exc).__name__}: {exc}"
            _cleanup_destination_table(db, staging_run)
            _fail(jobs_service, audit, job, staging_run, actor, message)
            db.commit()
            return {"status": "FAILED", "error": message}
    finally:
        db.close()


def _execute_materialization(db, redis_client, jobs_service, audit, job, staging_run, actor) -> dict:
    jobs_service.mark_running(job.id)
    now = datetime.now(timezone.utc)
    staging_run.materialization_phase = "PREPARING_SCHEMA"
    staging_run.updated_at = now
    db.commit()
    audit.record(
        actor=actor, action="staging_run.materialization_started", entity_type="STAGING_RUN",
        entity_id=staging_run.id,
    )
    db.commit()

    dataset = db.get(Dataset, staging_run.dataset_id)
    schema_row = db.get(Schema, dataset.schema_id)
    connection = db.get(Connection, schema_row.connection_id)
    connection_type = db.get(ConnectionType, connection.connection_type_id)
    vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)

    try:
        credential = vault.resolve(connection.credential_ref)
        provider = get_provider(
            connection_type.code, host=connection.host, port=connection.port, database=connection.database_name,
            username=credential.get("username", connection.username), password=credential.get("password", ""),
        )
    except _TERMINAL_SOURCE_ERRORS as exc:
        message = f"{type(exc).__name__}: {exc}"
        _fail(jobs_service, audit, job, staging_run, actor, message)
        db.commit()
        return {"status": "FAILED", "error": message}

    try:
        # resolve_key_context and everything else that touches `provider`
        # lives inside this try so the finally below always closes it, even
        # if metadata resolution itself fails.
        staging_service = StagingService(db)
        key_context = staging_service.resolve_key_context(dataset)
        column_names = [c.name for c in key_context.active_columns]

        # staging_data is created here defensively (also created by
        # migration 0025) — never depend on it being manually pre-provisioned.
        db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_pg_identifier(STAGING_SCHEMA_NAME)}"))
        db.commit()

        destination_table = build_destination_table_name(dataset.name, staging_run.id)
        qualified_table = f"{quote_pg_identifier(STAGING_SCHEMA_NAME)}.{quote_pg_identifier(destination_table)}"

        now = datetime.now(timezone.utc)
        staging_run.materialization_phase = "CREATING_TABLE"
        staging_run.destination_schema = STAGING_SCHEMA_NAME
        staging_run.destination_table = destination_table
        staging_run.updated_at = now
        db.commit()

        column_ddl = ", ".join(
            f"{quote_pg_identifier(c.name)} {normalized_type_to_pg_ddl(c.normalized_data_type, max_length=c.max_length, numeric_precision=c.numeric_precision, numeric_scale=c.numeric_scale)}"
            for c in key_context.active_columns
        )
        # DROP + CREATE (never CREATE TABLE IF NOT EXISTS) — a retry/
        # redelivery must never append into a half-filled table from a
        # prior attempt; run-scoped naming means this only ever affects
        # THIS run's own table, never another run's.
        db.execute(text(f"DROP TABLE IF EXISTS {qualified_table}"))
        db.execute(text(f"CREATE TABLE {qualified_table} ({column_ddl})"))
        db.commit()

        now = datetime.now(timezone.utc)
        staging_run.materialization_phase = "COPYING_SOURCE"
        staging_run.updated_at = now
        db.commit()

        source_row_count = provider.count_rows(schema_row.name, dataset.name)
        staging_run.source_row_count = source_row_count
        db.commit()

        insert_columns_sql = ", ".join(quote_pg_identifier(c) for c in column_names)
        param_names = [f"c{i}" for i in range(len(column_names))]
        values_sql = ", ".join(f":{p}" for p in param_names)
        insert_sql = text(f"INSERT INTO {qualified_table} ({insert_columns_sql}) VALUES ({values_sql})")

        order_by = key_context.key_column_names or None
        copied = 0
        for batch in provider.iter_rows(
            schema_row.name, dataset.name, column_names, settings.STAGING_MATERIALIZATION_BATCH_SIZE, order_by=order_by
        ):
            if jobs_service.is_cancel_requested(job.id):
                raise _MaterializationCancelled()

            db.execute(insert_sql, [{f"c{i}": row.get(name) for i, name in enumerate(column_names)} for row in batch])
            copied += len(batch)
            staging_run.copied_row_count = copied
            if source_row_count:
                staging_run.progress_percentage = min(99, int(copied * 100 / source_row_count))
            staging_run.updated_at = datetime.now(timezone.utc)
            db.commit()

        staging_run.materialized_row_count = copied
        db.commit()

        if copied != source_row_count:
            raise RowCountMismatchError(
                f"source_row_count ({source_row_count}) != materialized_row_count ({copied}) — "
                "the source table likely changed size during materialization"
            )

        now = datetime.now(timezone.utc)
        staging_run.materialization_phase = "APPLYING_CORRECTIONS"
        staging_run.updated_at = now
        db.commit()

        staging_records = db.execute(
            select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)
        ).scalars().all()
        columns_by_name = {c.name: c for c in key_context.active_columns}

        for record in staging_records:
            key_dict = parse_record_ref_to_key_dict(
                record.record_ref, key_strategy=key_context.effective_key_strategy,
                key_column_names=key_context.key_column_names,
            )
            if key_dict is None:
                # ROW_INDEX_FALLBACK (or no key at all) — this record cannot
                # be safely targeted in the materialized table, exactly the
                # same limitation that already leaves it RECORD_NOT_FOUND-
                # eligible in the audit layer. The row copied verbatim from
                # source is left as-is; nothing here treats that as an error.
                continue

            set_parts = []
            params: dict = {}
            for idx, field in enumerate(record.corrected_fields):
                column_name = field.get("column_name")
                if column_name is None:
                    continue
                column = columns_by_name.get(column_name)
                normalized_type = column.normalized_data_type if column is not None else "STRING"
                coerced = coerce_final_value(
                    field.get("final_value"), normalized_type=normalized_type, column_name=column_name
                )
                pname = f"s{idx}"
                set_parts.append(f"{quote_pg_identifier(column_name)} = :{pname}")
                params[pname] = coerced
            if not set_parts:
                continue

            where_parts = []
            for j, col in enumerate(key_context.key_column_names):
                value = key_dict.get(col)
                if value is None:
                    where_parts.append(f"{quote_pg_identifier(col)} IS NULL")
                else:
                    pname = f"k{j}"
                    where_parts.append(f"{quote_pg_identifier(col)} = :{pname}")
                    params[pname] = value

            update_sql = text(
                f"UPDATE {qualified_table} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}"
            )
            db.execute(update_sql, params)
        db.commit()

        now = datetime.now(timezone.utc)
        staging_run.materialization_phase = "VALIDATING"
        staging_run.updated_at = now
        db.commit()

    except _MaterializationCancelled:
        _cleanup_destination_table(db, staging_run)
        jobs_service.mark_cancelled(job.id)
        jobs_service.clear_cancel_flag(job.id)
        now = datetime.now(timezone.utc)
        staging_run.materialization_phase = "CANCELLED"
        staging_run.updated_at = now
        audit.record(
            actor=actor, action="staging_run.materialization_cancelled", entity_type="STAGING_RUN",
            entity_id=staging_run.id,
        )
        db.commit()
        return {"status": "CANCELLED"}
    except (_TERMINAL_SOURCE_ERRORS + (RowCountMismatchError, CorrectionCoercionError)) as exc:
        message = f"{type(exc).__name__}: {exc}"
        _cleanup_destination_table(db, staging_run)
        _fail(jobs_service, audit, job, staging_run, actor, message)
        db.commit()
        return {"status": "FAILED", "error": message}
    finally:
        provider.close()

    now = datetime.now(timezone.utc)
    staging_run.materialization_phase = "FINALIZING"
    staging_run.updated_at = now
    db.commit()

    staging_run.materialization_phase = "READY"
    staging_run.progress_percentage = 100
    staging_run.updated_at = datetime.now(timezone.utc)
    jobs_service.mark_completed(job.id)
    audit.record(
        actor=actor, action="staging_run.materialization_completed", entity_type="STAGING_RUN",
        entity_id=staging_run.id,
        metadata={
            "destination_schema": staging_run.destination_schema, "destination_table": staging_run.destination_table,
            "materialized_row_count": staging_run.materialized_row_count,
        },
    )
    db.commit()
    return {"status": "READY", "materialized_row_count": staging_run.materialized_row_count}


def _cleanup_destination_table(db, staging_run: StagingRun) -> None:
    """Best-effort — cleanup failing must never mask the original error, and
    must never leave the transaction unusable for the FAILED/CANCELLED
    write that follows it."""
    if staging_run.destination_schema is None or staging_run.destination_table is None:
        return
    try:
        db.rollback()
        qualified_table = (
            f"{quote_pg_identifier(staging_run.destination_schema)}.{quote_pg_identifier(staging_run.destination_table)}"
        )
        db.execute(text(f"DROP TABLE IF EXISTS {qualified_table}"))
        db.commit()
    except Exception:
        db.rollback()


def _fail(jobs_service, audit, job, staging_run, actor, message: str) -> None:
    jobs_service.mark_failed(job.id, message)
    now = datetime.now(timezone.utc)
    staging_run.materialization_phase = "FAILED"
    staging_run.materialization_error = message
    staging_run.updated_at = now
    audit.record(
        actor=actor, action="staging_run.materialization_failed", entity_type="STAGING_RUN", entity_id=staging_run.id,
        metadata={"error": message},
    )

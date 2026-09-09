import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import CredentialVaultError
from app.core.redis_client import get_redis_client
from app.db.models import Column, ColumnProfile, Connection, ConnectionType, Dataset, ProfileRun, Schema, User
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.jobs.service import JobsService
from app.modules.profiling.engine import compute_dataset_duplicate_percentage, profile_column
from app.modules.profiling.run_options import clear_include_top_values, get_include_top_values
from app.source_adapters.exceptions import (
    ExactStatsTimeoutError,
    SourceAuthenticationError,
    SourceQueryError,
    SourceSSLError,
    SourceTimeoutError,
    SourceUnreachableError,
)
from app.source_adapters.factory import get_provider

_TERMINAL_ERRORS = (
    SourceAuthenticationError,
    SourceTimeoutError,
    SourceUnreachableError,
    SourceSSLError,
    CredentialVaultError,
)


@celery_app.task(name="profiling.run_profile")
def run_profile(job_id: str, profile_run_id: str) -> dict:
    db = SessionLocal()
    try:
        redis_client = get_redis_client()
        jobs_service = JobsService(db, redis_client)
        job = jobs_service.get(uuid.UUID(job_id))
        profile_run = db.get(ProfileRun, uuid.UUID(profile_run_id))

        if (
            job.status in ("CANCELLED", "COMPLETED", "FAILED")
            or profile_run is None
            or profile_run.status in ("CANCELLED", "COMPLETED", "FAILED")
        ):
            # A redelivered/duplicate Celery message for a run that already
            # reached a terminal state. No-op — idempotency guard. Also
            # covers manual re-invocation by job_id/profile_run_id alone
            # (e.g. recovering a job stuck after a lost worker) after it was
            # already independently resolved (e.g. by the stale-job sweep).
            if profile_run is not None:
                clear_include_top_values(redis_client, profile_run.id)
            return {"status": job.status}

        # Recovered from Redis, not a task kwarg — reliably recoverable
        # regardless of how/how many times this task is invoked. See
        # app.modules.profiling.run_options for why.
        include_top_values = get_include_top_values(redis_client, profile_run.id)

        actor = db.get(User, job.created_by) if job.created_by else None
        dataset = db.get(Dataset, job.entity_id)
        audit = AuditingService(db)

        jobs_service.mark_running(job.id)
        now = datetime.now(timezone.utc)
        profile_run.status = "RUNNING"
        profile_run.started_at = now
        db.commit()
        audit.record(actor=actor, action="profiling.started", entity_type="DATASET", entity_id=dataset.id)
        db.commit()

        schema_row = db.get(Schema, dataset.schema_id)
        connection = db.get(Connection, schema_row.connection_id)
        connection_type = db.get(ConnectionType, connection.connection_type_id)
        vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)

        try:
            credential = vault.resolve(connection.credential_ref)
            provider = get_provider(
                connection_type.code,
                host=connection.host,
                port=connection.port,
                database=connection.database_name,
                username=credential.get("username", connection.username),
                password=credential.get("password", ""),
            )
        except _TERMINAL_ERRORS as exc:
            message = f"{type(exc).__name__}: {exc}"
            _fail(jobs_service, audit, job, profile_run, dataset, actor, message)
            db.commit()
            clear_include_top_values(redis_client, profile_run.id)
            return {"status": "FAILED", "error": message}

        # Snapshot currently-active columns once, at task start. A column
        # activated/deactivated mid-run by a concurrent Discovery run is a
        # known, accepted race — not resolved by this phase.
        active_columns = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
        ).scalars().all()
        column_names = [c.name for c in active_columns]

        try:
            try:
                exact_stats = (
                    provider.get_dataset_column_stats(schema_row.name, dataset.name, column_names)
                    if column_names
                    else {}
                )
            except ExactStatsTimeoutError:
                exact_stats = {}

            is_full_scan_requested = profile_run.sample_size is None
            if is_full_scan_requested:
                sample_size_to_request = dataset.row_count_estimate or settings.PROFILING_MAX_FULL_SCAN_ROWS
            else:
                sample_size_to_request = profile_run.sample_size

            sample_result = provider.sample_rows(
                schema_row.name,
                dataset.name,
                sample_size_to_request,
                row_count_estimate=dataset.row_count_estimate,
            )
        except (SourceTimeoutError, SourceQueryError) as exc:
            message = f"{type(exc).__name__}: {exc}"
            _fail(jobs_service, audit, job, profile_run, dataset, actor, message)
            db.commit()
            clear_include_top_values(redis_client, profile_run.id)
            return {"status": "FAILED", "error": message}
        finally:
            provider.close()

        rows = sample_result.rows
        column_success_count = 0
        column_failure_count = 0
        cancelled = False

        for column in active_columns:
            if jobs_service.is_cancel_requested(job.id):
                cancelled = True
                break

            try:
                sample_values = [row.get(column.name) for row in rows]
                profile_fields = profile_column(
                    column_name=column.name,
                    normalized_data_type=column.normalized_data_type,
                    sample_values=sample_values,
                    exact_stats=exact_stats.get(column.name),
                    include_top_values=include_top_values,
                )
                db.add(ColumnProfile(profile_run_id=profile_run.id, column_id=column.id, **profile_fields))
                db.commit()
                column_success_count += 1
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                column_failure_count += 1
                audit.record(
                    actor=actor,
                    action="profiling.column_failed",
                    entity_type="DATASET",
                    entity_id=dataset.id,
                    metadata={"column": column.name, "error": str(exc)},
                )
                db.commit()
                continue

        if cancelled:
            jobs_service.mark_cancelled(job.id)
            jobs_service.clear_cancel_flag(job.id)
            now = datetime.now(timezone.utc)
            profile_run.status = "CANCELLED"
            profile_run.completed_at = now
            audit.record(
                actor=actor,
                action="profiling.cancelled",
                entity_type="DATASET",
                entity_id=dataset.id,
                metadata={"columns_profiled": column_success_count},
            )
            db.commit()
            clear_include_top_values(redis_client, profile_run.id)
            return {"status": "CANCELLED"}

        duplicate_percentage = compute_dataset_duplicate_percentage(rows)

        profile_run.sample_size = len(rows)
        if sample_result.is_full_scan:
            profile_run.row_count = len(rows)
        else:
            db.refresh(dataset)
            profile_run.row_count = dataset.row_count_estimate
        profile_run.duplicate_percentage = duplicate_percentage
        # profile_run.null_percentage and profile_run.quality_score are
        # NEVER set — both remain NULL, reserved for a later phase.

        summary_message = None
        if column_failure_count > 0:
            summary_message = (
                f"{column_success_count}/{len(active_columns)} columns profiled, {column_failure_count} failed"
            )

        now = datetime.now(timezone.utc)
        profile_run.status = "COMPLETED"
        profile_run.completed_at = now
        profile_run.error_message = summary_message
        jobs_service.mark_completed(job.id, error_message=summary_message)

        audit.record(
            actor=actor,
            action="profiling.completed",
            entity_type="DATASET",
            entity_id=dataset.id,
            metadata={
                "columns_profiled": column_success_count,
                "columns_failed": column_failure_count,
                "sample_size": profile_run.sample_size,
                "is_full_scan": sample_result.is_full_scan,
            },
        )
        db.commit()
        clear_include_top_values(redis_client, profile_run.id)
        return {
            "status": "COMPLETED",
            "columns_profiled": column_success_count,
            "columns_failed": column_failure_count,
        }
    finally:
        db.close()


def _fail(jobs_service, audit, job, profile_run, dataset, actor, message: str) -> None:
    jobs_service.mark_failed(job.id, message)
    now = datetime.now(timezone.utc)
    profile_run.status = "FAILED"
    profile_run.error_message = message
    profile_run.completed_at = now
    audit.record(
        actor=actor,
        action="profiling.failed",
        entity_type="DATASET",
        entity_id=dataset.id,
        metadata={"error": message},
    )

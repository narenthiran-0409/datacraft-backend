import uuid

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import CredentialVaultError
from app.core.redis_client import get_redis_client
from app.db.models import Connection, ConnectionType, User
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.discovery.service import DiscoveryService
from app.modules.jobs.service import JobsService
from app.source_adapters.exceptions import (
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
_PER_DATASET_ERRORS = (SourceTimeoutError, SourceQueryError)


@celery_app.task(name="discovery.run_discovery")
def run_discovery(job_id: str) -> dict:
    db = SessionLocal()
    try:
        redis_client = get_redis_client()
        jobs_service = JobsService(db, redis_client)
        job = jobs_service.get(uuid.UUID(job_id))
        if job.status in ("CANCELLED", "COMPLETED", "FAILED"):
            # A redelivered/duplicate Celery message for a job that already
            # reached a terminal state (e.g. cancelled while still QUEUED,
            # before any worker consumed the original message). No-op.
            return {"status": job.status}

        actor = db.get(User, job.created_by) if job.created_by else None
        connection = db.get(Connection, job.entity_id)
        audit = AuditingService(db)

        jobs_service.mark_running(job.id)

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
            schemas = provider.list_schemas()
        except _TERMINAL_ERRORS as exc:
            message = f"{type(exc).__name__}: {exc}"
            jobs_service.mark_failed(job.id, message)
            audit.record(
                actor=actor,
                action="discovery.failed",
                entity_type="CONNECTION",
                entity_id=connection.id,
                metadata={"error": message},
            )
            db.commit()
            return {"status": "FAILED", "error": message}

        discovery_service = DiscoveryService(db)
        discovered_schema_names: set[str] = set()
        discovered_dataset_keys: set[tuple[uuid.UUID, str]] = set()
        dataset_success_count = 0
        dataset_failure_count = 0
        dataset_total_count = 0
        foreign_key_summary: list[dict] = []

        try:
            for schema_name in schemas:
                if jobs_service.is_cancel_requested(job.id):
                    jobs_service.mark_cancelled(job.id)
                    jobs_service.clear_cancel_flag(job.id)
                    db.commit()
                    return {"status": "CANCELLED"}

                schema = discovery_service.upsert_schema(connection.id, schema_name)
                discovered_schema_names.add(schema_name)
                db.commit()

                try:
                    datasets = provider.list_datasets(schema_name)
                except _PER_DATASET_ERRORS as exc:
                    db.rollback()
                    dataset_failure_count += 1
                    audit.record(
                        actor=actor,
                        action="discovery.dataset_failed",
                        entity_type="CONNECTION",
                        entity_id=connection.id,
                        metadata={"schema": schema_name, "stage": "list_datasets", "error": str(exc)},
                    )
                    db.commit()
                    continue

                for dataset_info in datasets:
                    if jobs_service.is_cancel_requested(job.id):
                        jobs_service.mark_cancelled(job.id)
                        jobs_service.clear_cancel_flag(job.id)
                        db.commit()
                        return {"status": "CANCELLED"}

                    dataset_total_count += 1
                    try:
                        columns_info = provider.get_columns(schema_name, dataset_info["name"])
                        primary_keys = provider.get_primary_keys(schema_name, dataset_info["name"])
                        row_count = provider.get_row_count(schema_name, dataset_info["name"])
                        foreign_keys = (
                            provider.get_foreign_keys(schema_name, dataset_info["name"])
                            if provider.get_capabilities().supports_foreign_keys
                            else []
                        )

                        discovery_service.upsert_dataset(
                            schema, dataset_info, columns_info, primary_keys, row_count
                        )
                        discovered_dataset_keys.add((schema.id, dataset_info["name"]))
                        db.commit()
                        dataset_success_count += 1
                        if foreign_keys:
                            foreign_key_summary.append(
                                {"schema": schema_name, "dataset": dataset_info["name"], "foreign_keys": foreign_keys}
                            )
                    except _PER_DATASET_ERRORS as exc:
                        db.rollback()
                        dataset_failure_count += 1
                        audit.record(
                            actor=actor,
                            action="discovery.dataset_failed",
                            entity_type="CONNECTION",
                            entity_id=connection.id,
                            metadata={"schema": schema_name, "dataset": dataset_info["name"], "error": str(exc)},
                        )
                        db.commit()
                        continue
        finally:
            provider.close()

        discovery_service.deactivate_missing(connection.id, discovered_schema_names, discovered_dataset_keys)
        db.commit()

        summary_message = None
        if dataset_failure_count > 0:
            summary_message = (
                f"{dataset_success_count}/{dataset_total_count} datasets discovered, "
                f"{dataset_failure_count} failed"
            )

        jobs_service.mark_completed(job.id, error_message=summary_message)
        audit.record(
            actor=actor,
            action="discovery.completed",
            entity_type="CONNECTION",
            entity_id=connection.id,
            metadata={
                "schemas": sorted(discovered_schema_names),
                "datasets_discovered": dataset_success_count,
                "datasets_failed": dataset_failure_count,
                "foreign_keys": foreign_key_summary,
            },
        )
        db.commit()
        return {
            "status": "COMPLETED",
            "datasets_discovered": dataset_success_count,
            "datasets_failed": dataset_failure_count,
        }
    finally:
        db.close()

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import CredentialVaultError
from app.core.redis_client import get_redis_client
from app.db.models import (
    Column,
    Connection,
    ConnectionType,
    Dataset,
    DatasetKeyColumn,
    Rule,
    RuleAssignment,
    RuleAssignmentColumn,
    RuleVersion,
    Schema,
    User,
    ValidationFailure,
    ValidationMetric,
    ValidationResult,
    ValidationRun,
    ValidationTemplate,
)
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.jobs.service import JobsService
from app.modules.validation.engine import get_evaluator, is_supported_rule_type
from app.modules.validation.record_ref import compute_source_row_hash, generate_record_ref
from app.source_adapters.exceptions import (
    ExactStatsTimeoutError,
    SourceAuthenticationError,
    SourceQueryError,
    SourceSSLError,
    SourceTimeoutError,
    SourceUnreachableError,
)
from app.source_adapters.factory import get_provider
from app.source_adapters.timeout import with_timeout

_TERMINAL_ERRORS = (
    SourceAuthenticationError,
    SourceTimeoutError,
    SourceUnreachableError,
    SourceSSLError,
    CredentialVaultError,
)

# Rule types whose evaluation mode requires an exact (full-scan) row pull,
# per the approved Phase 5 rule-type taxonomy.
_EXACT_EVALUATION_RULE_TYPES = frozenset({"COMPLETENESS", "UNIQUENESS", "DUPLICATE", "CROSS_COLUMN"})
# Rule types expensive enough to warrant VALIDATION_DATASET_TIMEOUT_SECONDS.
_TIMEOUT_BOUNDED_RULE_TYPES = frozenset({"CROSS_COLUMN", "DUPLICATE"})

_SEVERITY_FAIL = {"CRITICAL", "HIGH"}
_SEVERITY_WARN = {"MEDIUM", "LOW"}


class _ValidationCancelled(Exception):
    pass


def _round_score(passed: int, total: int) -> Decimal | None:
    if total == 0:
        return None
    return Decimal(str(round(100.0 * passed / total, 2)))


@celery_app.task(name="validation.run_validation")
def run_validation(job_id: str, validation_run_id: str) -> dict:
    db = SessionLocal()
    try:
        redis_client = get_redis_client()
        jobs_service = JobsService(db, redis_client)
        job = jobs_service.get(uuid.UUID(job_id))
        validation_run = db.get(ValidationRun, uuid.UUID(validation_run_id))

        if (
            job.status in ("CANCELLED", "COMPLETED", "FAILED")
            or validation_run is None
            or validation_run.status in ("CANCELLED", "COMPLETED", "FAILED")
        ):
            # Idempotency guard — same rationale as run_profile: a
            # redelivered/duplicate message, or a manual re-invocation by
            # job_id/validation_run_id alone, for a run already resolved.
            return {"status": job.status}

        actor = db.get(User, job.created_by) if job.created_by else None
        dataset = db.get(Dataset, job.entity_id)
        audit = AuditingService(db)

        try:
            return _execute_validation(db, redis_client, jobs_service, audit, job, validation_run, dataset, actor)
        except Exception as exc:
            # Catch-all safety net. Every error this task anticipates
            # (source connection/query/timeout errors, explicit
            # cancellation) is already handled and returned from inside
            # _execute_validation() itself. Reaching here means something
            # genuinely unexpected happened — without this, job/validation_run
            # stay stuck at RUNNING forever with no error recorded anywhere,
            # since both were already committed to RUNNING before this point
            # and nothing else ever revisits them.
            db.rollback()
            message = f"{type(exc).__name__}: {exc}"
            _fail(jobs_service, audit, job, validation_run, dataset, actor, message)
            db.commit()
            return {"status": "FAILED", "error": message}
    finally:
        db.close()


def _execute_validation(db, redis_client, jobs_service, audit, job, validation_run, dataset, actor) -> dict:
    jobs_service.mark_running(job.id)
    now = datetime.now(timezone.utc)
    validation_run.status = "RUNNING"
    validation_run.started_at = now
    db.commit()
    audit.record(actor=actor, action="validation.started", entity_type="DATASET", entity_id=dataset.id)
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
        _fail(jobs_service, audit, job, validation_run, dataset, actor, message)
        db.commit()
        return {"status": "FAILED", "error": message}

    # Resolve enabled assignments against the live table (may have
    # changed since enqueue) — each entry carries the assignment plus
    # its resolved rule_version/rule (for rule_type/severity/definition).
    assignments = list(
        db.execute(
            select(RuleAssignment).where(
                RuleAssignment.dataset_id == dataset.id, RuleAssignment.is_enabled.is_(True)
            )
        ).scalars()
    )
    assignment_info = []
    for assignment in assignments:
        rule_version = db.get(RuleVersion, assignment.rule_version_id)
        rule = db.get(Rule, rule_version.rule_id)
        assignment_info.append((assignment, rule_version, rule))

    active_columns = db.execute(
        select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
    ).scalars().all()
    columns_by_id = {c.id: c for c in active_columns}
    column_names = [c.name for c in active_columns]

    key_columns = db.execute(
        select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id).order_by(DatasetKeyColumn.ordinal)
    ).scalars().all()
    key_column_names = [
        columns_by_id[kc.column_id].name for kc in key_columns if kc.column_id in columns_by_id
    ]
    effective_key_strategy = dataset.key_strategy if key_column_names else "ROW_INDEX_FALLBACK"

    requires_full_scan = any(rule.rule_type in _EXACT_EVALUATION_RULE_TYPES for _, _, rule in assignment_info)
    if requires_full_scan:
        sample_size_to_request = dataset.row_count_estimate or settings.PROFILING_MAX_FULL_SCAN_ROWS
    else:
        sample_size_to_request = settings.PROFILING_DEFAULT_SAMPLE_SIZE

    try:
        sample_result = provider.sample_rows(
            schema_row.name, dataset.name, sample_size_to_request, row_count_estimate=dataset.row_count_estimate
        )

        pushdown_column_names = {
            columns_by_id[a.column_id].name
            for a, _, rule in assignment_info
            if rule.rule_type in ("COMPLETENESS", "UNIQUENESS") and a.column_id in columns_by_id
        }
        try:
            exact_stats = (
                provider.get_dataset_column_stats(schema_row.name, dataset.name, list(pushdown_column_names))
                if pushdown_column_names
                else {}
            )
        except ExactStatsTimeoutError:
            exact_stats = {}
    except (SourceTimeoutError, SourceQueryError) as exc:
        message = f"{type(exc).__name__}: {exc}"
        _fail(jobs_service, audit, job, validation_run, dataset, actor, message)
        db.commit()
        return {"status": "FAILED", "error": message}
    finally:
        provider.close()

    rows = sample_result.rows
    total_rows = len(rows)
    validation_run.sample_size = total_rows

    if jobs_service.is_cancel_requested(job.id):
        _cancel(jobs_service, audit, job, validation_run, dataset, actor)
        db.commit()
        return {"status": "CANCELLED"}

    if total_rows == 0:
        # Approved zero-row behavior: quality_score stays NULL,
        # datasets.last_quality_score is NOT overwritten, but
        # last_validated_at IS updated (a validation did run, even
        # though it evaluated nothing).
        now = datetime.now(timezone.utc)
        validation_run.status = "COMPLETED"
        validation_run.completed_at = now
        validation_run.total_rows = 0
        validation_run.quality_score = None
        if validation_run.started_at is not None:
            validation_run.duration_ms = int((now - validation_run.started_at).total_seconds() * 1000)
        jobs_service.mark_completed(job.id)
        db.add(ValidationMetric(validation_run_id=validation_run.id, metric_name="total_rows", metric_value=0))
        db.add(
            ValidationMetric(
                validation_run_id=validation_run.id, metric_name="failure_storage_truncated", metric_value=0
            )
        )
        dataset.last_validated_at = now
        audit.record(
            actor=actor,
            action="validation.completed",
            entity_type="DATASET",
            entity_id=dataset.id,
            metadata={"total_rows": 0},
        )
        db.commit()
        return {"status": "COMPLETED", "total_rows": 0}

    failures_by_row: dict[int, list[tuple]] = {}
    unsupported_counts: dict[str, int] = {}

    def _do_evaluation() -> None:
        for assignment, rule_version, rule in assignment_info:
            if jobs_service.is_cancel_requested(job.id):
                raise _ValidationCancelled()

            if not is_supported_rule_type(rule.rule_type):
                unsupported_counts[rule.rule_type] = unsupported_counts.get(rule.rule_type, 0) + 1
                continue

            evaluator = get_evaluator(rule.rule_type)
            definition = rule_version.definition or {}

            if rule.rule_type in ("COMPLETENESS", "UNIQUENESS"):
                column = columns_by_id.get(assignment.column_id)
                if column is None:
                    continue
                row_failures = evaluator(
                    rows=rows, column_name=column.name, definition=definition,
                    exact_stats=exact_stats.get(column.name),
                )
            elif rule.rule_type == "DUPLICATE":
                row_failures = evaluator(rows=rows, definition=definition)
            elif rule.rule_type in ("RANGE", "PATTERN"):
                column = columns_by_id.get(assignment.column_id)
                if column is None:
                    continue
                row_failures = evaluator(rows=rows, column_name=column.name, definition=definition)
            elif rule.rule_type == "CROSS_COLUMN":
                rac_rows = db.execute(
                    select(RuleAssignmentColumn)
                    .where(RuleAssignmentColumn.rule_assignment_id == assignment.id)
                    .order_by(RuleAssignmentColumn.ordinal)
                ).scalars().all()
                cross_column_names = [
                    columns_by_id[rac.column_id].name for rac in rac_rows if rac.column_id in columns_by_id
                ]
                row_failures = evaluator(rows=rows, column_names_in_order=cross_column_names, definition=definition)
            else:
                continue

            for row_idx, failure in row_failures.items():
                failures_by_row.setdefault(row_idx, []).append((assignment, rule_version, failure))

    needs_timeout = any(rule.rule_type in _TIMEOUT_BOUNDED_RULE_TYPES for _, _, rule in assignment_info)
    try:
        if needs_timeout:
            with_timeout(settings.VALIDATION_DATASET_TIMEOUT_SECONDS)(_do_evaluation)()
        else:
            _do_evaluation()
    except SourceTimeoutError as exc:
        message = (
            f"Validation timed out after {settings.VALIDATION_DATASET_TIMEOUT_SECONDS}s: {exc}"
        )
        _fail(jobs_service, audit, job, validation_run, dataset, actor, message)
        audit.record(
            actor=actor,
            action="validation.failed",
            entity_type="DATASET",
            entity_id=dataset.id,
            metadata={"reason": "timeout", "timeout_seconds": settings.VALIDATION_DATASET_TIMEOUT_SECONDS},
        )
        db.commit()
        return {"status": "FAILED", "error": message}
    except _ValidationCancelled:
        _cancel(jobs_service, audit, job, validation_run, dataset, actor)
        db.commit()
        return {"status": "CANCELLED"}

    passed_rows = 0
    warning_rows = 0
    failed_rows = 0
    failures_persisted_count = 0

    for row_idx in range(total_rows):
        # BUG FIX (found via live E2E cancel testing): cancellation was only ever
        # checked before evaluation started (above), never during this loop — which
        # is where the time actually goes for any dataset large enough to matter
        # (one db.flush() per row). A cancel request arriving after the pre-evaluation
        # checkpoint was silently ignored: the API returned 200 and the redis flag was
        # genuinely set, but the run completed anyway. Checked periodically (not every
        # row) to avoid a redis round-trip per row on large datasets.
        if row_idx % 500 == 0 and jobs_service.is_cancel_requested(job.id):
            db.rollback()
            _cancel(jobs_service, audit, job, validation_run, dataset, actor)
            db.commit()
            return {"status": "CANCELLED"}

        row_failure_entries = failures_by_row.get(row_idx, [])
        failure_count = len(row_failure_entries)

        if failure_count == 0:
            row_status = "PASSED"
            passed_rows += 1
        else:
            severities = {rv.severity for _, rv, _ in row_failure_entries}
            if severities & _SEVERITY_FAIL:
                row_status = "FAILED"
                failed_rows += 1
            else:
                row_status = "WARNING"
                warning_rows += 1

        row = rows[row_idx]
        record_ref = generate_record_ref(
            key_strategy=effective_key_strategy, key_column_names_in_order=key_column_names,
            row=row, row_index=row_idx,
        )
        source_row_hash = compute_source_row_hash(row=row, column_names_in_order=column_names)

        result_row = ValidationResult(
            validation_run_id=validation_run.id, record_ref=record_ref, row_index=row_idx,
            status=row_status, failure_count=failure_count, source_row_hash=source_row_hash,
        )
        db.add(result_row)
        db.flush()

        for assignment, rule_version, failure in row_failure_entries:
            # VALIDATION_MAX_FAILURES caps PERSISTED validation_failures
            # rows only — evaluation already completed above and
            # aggregate counts (passed/warning/failed_rows) above are
            # unaffected by this cap.
            if failures_persisted_count >= settings.VALIDATION_MAX_FAILURES:
                continue
            db.add(
                ValidationFailure(
                    validation_result_id=result_row.id, validation_run_id=validation_run.id,
                    rule_assignment_id=assignment.id, column_id=assignment.column_id,
                    severity=rule_version.severity, failed_value=failure.failed_value,
                    expected_value=failure.expected_value, reason=failure.reason,
                )
            )
            failures_persisted_count += 1

    truncated = 1 if failures_persisted_count >= settings.VALIDATION_MAX_FAILURES else 0
    db.add(
        ValidationMetric(
            validation_run_id=validation_run.id, metric_name="failure_storage_truncated", metric_value=truncated
        )
    )
    db.add(ValidationMetric(validation_run_id=validation_run.id, metric_name="total_rows", metric_value=total_rows))
    for rule_type, count in unsupported_counts.items():
        db.add(
            ValidationMetric(
                validation_run_id=validation_run.id, metric_name="unsupported_rule_type_skipped",
                metric_group=rule_type, metric_value=count,
            )
        )

    quality_score = _round_score(passed_rows, total_rows)

    now = datetime.now(timezone.utc)
    validation_run.status = "COMPLETED"
    validation_run.completed_at = now
    validation_run.total_rows = total_rows
    validation_run.passed_rows = passed_rows
    validation_run.warning_rows = warning_rows
    validation_run.failed_rows = failed_rows
    validation_run.quality_score = quality_score
    if validation_run.started_at is not None:
        validation_run.duration_ms = int((now - validation_run.started_at).total_seconds() * 1000)

    # quality_score is never NULL here (total_rows > 0 in this branch),
    # so datasets.last_quality_score is always written in this branch —
    # the "do not overwrite" rule applies only to the zero-row branch above.
    dataset.last_validated_at = now
    dataset.last_quality_score = quality_score

    if validation_run.template_id is not None:
        template = db.get(ValidationTemplate, validation_run.template_id)
        if template is not None:
            template.last_quality_score = quality_score
            template.last_run_id = validation_run.id

    jobs_service.mark_completed(job.id)
    audit.record(
        actor=actor,
        action="validation.completed",
        entity_type="DATASET",
        entity_id=dataset.id,
        metadata={
            "total_rows": total_rows, "passed_rows": passed_rows, "warning_rows": warning_rows,
            "failed_rows": failed_rows, "quality_score": str(quality_score) if quality_score is not None else None,
            "failure_storage_truncated": bool(truncated),
        },
    )
    db.commit()
    return {
        "status": "COMPLETED", "total_rows": total_rows, "passed_rows": passed_rows,
        "warning_rows": warning_rows, "failed_rows": failed_rows,
    }


def _fail(jobs_service, audit, job, validation_run, dataset, actor, message: str) -> None:
    jobs_service.mark_failed(job.id, message)
    now = datetime.now(timezone.utc)
    validation_run.status = "FAILED"
    validation_run.error_message = message
    validation_run.completed_at = now
    audit.record(
        actor=actor, action="validation.failed", entity_type="DATASET", entity_id=dataset.id,
        metadata={"error": message},
    )


def _cancel(jobs_service, audit, job, validation_run, dataset, actor) -> None:
    jobs_service.mark_cancelled(job.id)
    jobs_service.clear_cancel_flag(job.id)
    now = datetime.now(timezone.utc)
    validation_run.status = "CANCELLED"
    validation_run.completed_at = now
    audit.record(
        actor=actor, action="validation.cancelled", entity_type="DATASET", entity_id=dataset.id,
    )

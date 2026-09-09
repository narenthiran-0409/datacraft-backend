import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    ApprovalNotApprovedError,
    CredentialVaultError,
    NoEligibleIssuesError,
    ReviewRunArchivedError,
    ReviewRunNotFoundError,
    StagingAlreadyInProgressError,
    StagingRecordCountExceedsLimitError,
    StagingRunNotFoundError,
)
from app.core.redis_client import get_redis_client
from app.db.models import (
    ApprovalDecision,
    ApprovalDecisionIssue,
    ApprovalRequest,
    Column,
    Connection,
    ConnectionType,
    Correction,
    Dataset,
    DatasetKeyColumn,
    Issue,
    ReviewRun,
    Schema,
    StagingRecord,
    StagingRun,
    User,
    ValidationFailure,
    ValidationResult,
)
from app.modules.audit.service import AuditingService
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.lineage.service import LineageService
from app.modules.staging.record_builder import (
    ScopeItem,
    StagingIntegrityViolationError,
    build_corrected_fields,
    build_row_snapshot,
    classify_drift,
    compute_staging_row_hash,
    group_by_record_ref,
    parse_record_ref_to_key_dict,
)
from app.modules.validation.record_ref import generate_record_ref
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
    SourceQueryError,
    CredentialVaultError,
)
_BUILD_FAILURE_ERRORS = _TERMINAL_ERRORS + (StagingIntegrityViolationError,)


class StagingService:
    """Strictly read-only with respect to Phase 1-7 data: never writes to
    approval_requests, approval_decisions, approval_decision_issues,
    corrections, or issues, under any circumstance."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._audit = AuditingService(db)
        self._lineage = LineageService(db)

    def get(self, staging_run_id: uuid.UUID) -> StagingRun:
        staging_run = self._db.get(StagingRun, staging_run_id)
        if staging_run is None:
            raise StagingRunNotFoundError(f"Staging run {staging_run_id} not found")
        return staging_run

    def list_records(self, staging_run_id: uuid.UUID, *, drift_only: bool) -> list[StagingRecord]:
        self.get(staging_run_id)
        stmt = select(StagingRecord).where(StagingRecord.staging_run_id == staging_run_id)
        if drift_only:
            stmt = stmt.where(StagingRecord.source_drift_status != "UNCHANGED")
        stmt = stmt.order_by(StagingRecord.created_at)
        return list(self._db.execute(stmt).scalars())

    def _resolve_approved_scope(self, approval_request_id: uuid.UUID) -> dict[str, list[ScopeItem]]:
        rows = self._db.execute(
            select(Issue, Correction)
            .join(ApprovalDecisionIssue, ApprovalDecisionIssue.issue_id == Issue.id)
            .join(ApprovalDecision, ApprovalDecision.id == ApprovalDecisionIssue.approval_decision_id)
            .outerjoin(Correction, Correction.issue_id == Issue.id)
            .where(ApprovalDecision.approval_request_id == approval_request_id)
        ).all()
        pairs = [
            (
                issue.record_ref,
                ScopeItem(
                    issue_id=issue.id, column_id=issue.column_id, original_value=issue.original_value,
                    correction_final_value=correction.final_value if correction is not None else None,
                ),
            )
            for issue, correction in rows
        ]
        return group_by_record_ref(pairs)

    def trigger(self, review_run_id: uuid.UUID, actor: User) -> StagingRun:
        # Locked decision 3: row lock BEFORE checking for an existing
        # BUILDING attempt and BEFORE creating the new staging_runs row.
        review_run = self._db.execute(
            select(ReviewRun).where(ReviewRun.id == review_run_id).with_for_update()
        ).scalar_one_or_none()
        if review_run is None:
            raise ReviewRunNotFoundError(f"Review run {review_run_id} not found")

        if review_run.status == "ARCHIVED":
            raise ReviewRunArchivedError(f"Review run {review_run_id} is archived")

        # Locked decision 5: most recently CREATED APPROVED request.
        approval_request = self._db.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.review_run_id == review_run_id, ApprovalRequest.status == "APPROVED")
            .order_by(ApprovalRequest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if approval_request is None:
            raise ApprovalNotApprovedError(f"No APPROVED approval request exists for review run {review_run_id}")

        groups = self._resolve_approved_scope(approval_request.id)
        if not groups:
            raise NoEligibleIssuesError(f"Review run {review_run_id} has zero eligible approved issues")
        if len(groups) > settings.MAX_SYNCHRONOUS_STAGING_RECORDS:
            raise StagingRecordCountExceedsLimitError(
                f"Eligible record count ({len(groups)}) exceeds MAX_SYNCHRONOUS_STAGING_RECORDS "
                f"({settings.MAX_SYNCHRONOUS_STAGING_RECORDS})"
            )

        existing_building = self._db.execute(
            select(StagingRun).where(StagingRun.review_run_id == review_run_id, StagingRun.status == "BUILDING")
        ).scalar_one_or_none()
        if existing_building is not None:
            raise StagingAlreadyInProgressError(
                f"Staging attempt {existing_building.id} is already BUILDING for this review run"
            )

        validation_run_id = review_run.validation_run_id
        from app.db.models import ValidationRun

        dataset_id = self._db.execute(
            select(ValidationRun.dataset_id).where(ValidationRun.id == validation_run_id)
        ).scalar_one()

        max_attempt = self._db.execute(
            select(func.max(StagingRun.attempt_number)).where(StagingRun.review_run_id == review_run_id)
        ).scalar_one()
        next_attempt = (max_attempt or 0) + 1

        previous_current = self._db.execute(
            select(StagingRun).where(StagingRun.review_run_id == review_run_id, StagingRun.is_current.is_(True))
        ).scalar_one_or_none()
        if previous_current is not None:
            previous_current.is_current = False

        now = datetime.now(timezone.utc)
        staging_run = StagingRun(
            review_run_id=review_run_id, dataset_id=dataset_id, attempt_number=next_attempt, is_current=True,
            status="BUILDING", created_by=actor.id, started_at=now,
        )
        self._db.add(staging_run)
        self._db.flush()

        # Phase 10 touch point 7 (additive-only): APPROVAL_REQUEST -> STAGING_RUN,
        # written at row creation, regardless of eventual BUILDING/READY/FAILED
        # outcome.
        self._lineage.record_edge(
            "APPROVAL_REQUEST", approval_request.id, "STAGING_RUN", staging_run.id, "STAGED_INTO"
        )

        self._audit.record(
            actor=actor, action="staging_run.created", entity_type="STAGING_RUN", entity_id=staging_run.id,
            metadata={
                "review_run_id": str(review_run_id), "approval_request_id": str(approval_request.id),
                "attempt_number": next_attempt,
            },
        )
        self._db.commit()
        self._db.refresh(staging_run)

        try:
            self._build_records(staging_run, dataset_id, groups)
        except _BUILD_FAILURE_ERRORS as exc:
            now = datetime.now(timezone.utc)
            staging_run.status = "FAILED"
            staging_run.error_message = f"{type(exc).__name__}: {exc}"
            staging_run.completed_at = now
            staging_run.updated_at = now
            self._audit.record(
                actor=actor, action="staging_run.failed", entity_type="STAGING_RUN", entity_id=staging_run.id,
                metadata={
                    "review_run_id": str(review_run_id), "attempt_number": next_attempt,
                    "error_type": type(exc).__name__,
                },
            )
            self._db.commit()
            self._db.refresh(staging_run)
            return staging_run

        created_records = self._db.execute(
            select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)
        ).scalars().all()
        now = datetime.now(timezone.utc)
        staging_run.status = "READY"
        staging_run.record_count = len(created_records)
        staging_run.field_count = sum(len(r.corrected_fields) for r in created_records)
        staging_run.has_source_drift = any(r.source_drift_status != "UNCHANGED" for r in created_records)
        staging_run.completed_at = now
        staging_run.updated_at = now

        self._audit.record(
            actor=actor, action="staging_run.completed", entity_type="STAGING_RUN", entity_id=staging_run.id,
            metadata={
                "review_run_id": str(review_run_id), "attempt_number": next_attempt,
                "record_count": staging_run.record_count, "field_count": staging_run.field_count,
                "has_source_drift": staging_run.has_source_drift,
            },
        )
        self._db.commit()
        self._db.refresh(staging_run)
        return staging_run

    def _build_records(
        self, staging_run: StagingRun, dataset_id: uuid.UUID, groups: dict[str, list[ScopeItem]]
    ) -> None:
        dataset = self._db.get(Dataset, dataset_id)
        schema_row = self._db.get(Schema, dataset.schema_id)
        connection = self._db.get(Connection, schema_row.connection_id)
        connection_type = self._db.get(ConnectionType, connection.connection_type_id)
        redis_client = get_redis_client()
        vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)

        provider = None
        try:
            credential = vault.resolve(connection.credential_ref)
            provider = get_provider(
                connection_type.code, host=connection.host, port=connection.port, database=connection.database_name,
                username=credential.get("username", connection.username), password=credential.get("password", ""),
            )

            # No ordering dependency here (unlike Phase 5's hash input) —
            # this is a plain lookup dict, order-independent by construction.
            active_columns = self._db.execute(
                select(Column).where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
            ).scalars().all()
            columns_by_id = {c.id: c for c in active_columns}

            key_columns = self._db.execute(
                select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id).order_by(DatasetKeyColumn.ordinal)
            ).scalars().all()
            key_column_names = [
                columns_by_id[kc.column_id].name for kc in key_columns if kc.column_id in columns_by_id
            ]
            effective_key_strategy = dataset.key_strategy if key_column_names else "ROW_INDEX_FALLBACK"

            record_refs_in_order = list(groups.keys())
            fetchable_key_dicts = []
            fetchable_record_refs = []
            for record_ref in record_refs_in_order:
                key_dict = parse_record_ref_to_key_dict(
                    record_ref, key_strategy=effective_key_strategy, key_column_names=key_column_names
                )
                if key_dict is not None:
                    fetchable_key_dicts.append(key_dict)
                    fetchable_record_refs.append(record_ref)

            # Single batched query for the whole set — never one per record.
            fetched_rows = provider.fetch_rows_by_keys(schema_row.name, dataset.name, fetchable_key_dicts)

            fetched_by_record_ref: dict[str, dict] = {}
            for row in fetched_rows:
                rr = generate_record_ref(
                    key_strategy=effective_key_strategy, key_column_names_in_order=key_column_names,
                    row=row, row_index=0,
                )
                fetched_by_record_ref[rr] = row
        finally:
            if provider is not None:
                provider.close()

        for record_ref in record_refs_in_order:
            items = groups[record_ref]
            corrected_fields = build_corrected_fields(items, {cid: c.name for cid, c in columns_by_id.items()})

            first_issue = self._db.get(Issue, items[0].issue_id)
            validation_failure = self._db.get(ValidationFailure, first_issue.validation_failure_id)
            validation_result = self._db.get(ValidationResult, validation_failure.validation_result_id)
            hash_at_validation = validation_result.source_row_hash

            fetched_row = fetched_by_record_ref.get(record_ref)
            # Phase-8-owned reproducible hash — never compared against
            # hash_at_validation (that comparison is explicitly disabled;
            # see the corrected drift-detection design). Preserved purely
            # so a LATER re-stage of this same review run can reliably
            # compare its own freshly-computed hash against THIS attempt's
            # source_row_hash_at_staging (a Phase-8-to-Phase-8 comparison).
            hash_at_staging = compute_staging_row_hash(fetched_row) if fetched_row is not None else None

            # Corrected-field-level drift classification ONLY — never a
            # whole-row hash comparison. See record_builder.classify_drift.
            drift_status, drift_fields = classify_drift(
                fetched_row_found=fetched_row is not None, corrected_fields=corrected_fields,
                fetched_row=fetched_row,
            )
            row_snapshot = build_row_snapshot(fetched_row, corrected_fields)

            self._db.add(
                StagingRecord(
                    staging_run_id=staging_run.id, record_ref=record_ref, row_snapshot=row_snapshot,
                    corrected_fields=corrected_fields, source_row_hash_at_validation=hash_at_validation,
                    source_row_hash_at_staging=hash_at_staging, source_drift_status=drift_status,
                    source_drift_fields=drift_fields,
                )
            )
            # Committed per-record so a later failure (e.g. an integrity
            # violation on a subsequent group) leaves earlier records
            # intact — the same partial-progress pattern already
            # established in Discovery and Profiling.
            self._db.commit()

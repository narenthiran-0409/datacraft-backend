import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
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
    StagingRunNotMaterializedError,
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
    Job,
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
from app.modules.staging.destination_naming import STAGING_SCHEMA_NAME, quote_pg_identifier
from app.modules.staging.type_mapping import normalized_type_to_pg_ddl
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


@dataclass(frozen=True)
class KeyContext:
    active_columns: list[Column]
    columns_by_id: dict[uuid.UUID, Column]
    key_column_names: list[str]
    effective_key_strategy: str


@dataclass(frozen=True)
class DestinationColumnInfo:
    name: str
    normalized_data_type: str
    staging_data_type: str


@dataclass(frozen=True)
class DestinationMetadata:
    staging_run: StagingRun
    columns: list[DestinationColumnInfo]
    approved_correction_count: int
    affected_row_count: int


@dataclass(frozen=True)
class MaterializedPreviewResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    limit: int
    offset: int
    has_more: bool
    # record_ref -> that record's corrected_fields (same shape as
    # StagingRecord.corrected_fields) — authoritative change metadata for
    # UI highlighting, sourced from staging_records, never recomputed.
    corrected_fields_by_record_ref: dict[str, list[dict[str, Any]]]
    # Enough for the caller to derive each returned row's record_ref via
    # app.modules.validation.record_ref.generate_record_ref — never a
    # frontend record_ref heuristic.
    key_strategy: str
    key_column_names: list[str]


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

    def resolve_key_context(self, dataset: Dataset) -> KeyContext:
        """Shared by _build_records (the affected-record audit layer) and
        the Phase 4.12 materialization task — the single place active
        columns / effective key strategy / key column names are resolved
        for a dataset, so both consumers agree on row identity."""
        active_columns = self._db.execute(
            select(Column)
            .where(Column.dataset_id == dataset.id, Column.is_active.is_(True))
            .order_by(Column.ordinal_position)
        ).scalars().all()
        columns_by_id = {c.id: c for c in active_columns}

        key_columns = self._db.execute(
            select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id).order_by(DatasetKeyColumn.ordinal)
        ).scalars().all()
        key_column_names = [
            columns_by_id[kc.column_id].name for kc in key_columns if kc.column_id in columns_by_id
        ]
        effective_key_strategy = dataset.key_strategy if key_column_names else "ROW_INDEX_FALLBACK"
        return KeyContext(
            active_columns=list(active_columns), columns_by_id=columns_by_id,
            key_column_names=key_column_names, effective_key_strategy=effective_key_strategy,
        )

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

        # Phase 4.12 — a successful affected-record audit build additionally
        # enqueues the full-dataset materialization job. This is additive
        # only: staging_run.status above is unchanged (still means exactly
        # "the audit-layer build outcome", nothing here alters it), and
        # trigger()'s own return type/signature is unchanged too — the route
        # dispatches the Celery task using the job_id populated here. A run
        # whose audit build FAILED (the early-return branch above) never
        # reaches this point, so it never gets a materialization job — there
        # is nothing yet to materialize corrections from.
        job = Job(
            job_type="STAGING_BUILD", entity_type="STAGING_RUN", entity_id=staging_run.id, created_by=actor.id
        )
        self._db.add(job)
        self._db.flush()
        staging_run.job_id = job.id
        staging_run.materialization_phase = "QUEUED"

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

            key_context = self.resolve_key_context(dataset)
            columns_by_id = key_context.columns_by_id
            key_column_names = key_context.key_column_names
            effective_key_strategy = key_context.effective_key_strategy

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

    def get_materialized(self, staging_run_id: uuid.UUID) -> StagingRun:
        """Returns the staging run, raising StagingRunNotMaterializedError
        (never crashing) when it has no physical staging_data table yet —
        either a pre-4.12 historical run, or one whose materialization job
        hasn't reached CREATING_TABLE yet."""
        staging_run = self.get(staging_run_id)
        if staging_run.destination_table is None:
            raise StagingRunNotMaterializedError(
                f"Staging run {staging_run_id} has no materialized dataset "
                f"(phase={staging_run.materialization_phase!r})"
            )
        return staging_run

    def get_destination_metadata(self, staging_run_id: uuid.UUID) -> DestinationMetadata:
        staging_run = self.get_materialized(staging_run_id)
        dataset = self._db.get(Dataset, staging_run.dataset_id)
        key_context = self.resolve_key_context(dataset)
        columns = [
            DestinationColumnInfo(
                name=c.name, normalized_data_type=c.normalized_data_type,
                staging_data_type=normalized_type_to_pg_ddl(
                    c.normalized_data_type, max_length=c.max_length,
                    numeric_precision=c.numeric_precision, numeric_scale=c.numeric_scale,
                ),
            )
            for c in key_context.active_columns
        ]
        return DestinationMetadata(
            staging_run=staging_run, columns=columns,
            approved_correction_count=staging_run.field_count, affected_row_count=staging_run.record_count,
        )

    def preview_materialized(
        self, staging_run_id: uuid.UUID, *, row_filter: str, limit: int, offset: int
    ) -> MaterializedPreviewResult:
        """Backend-authoritative preview of the MATERIALIZED physical table
        — never staging_records.row_snapshot, never a frontend record_ref
        heuristic. row_filter is one of "ALL", "CHANGED", "UNCHANGED".

        CHANGED/UNCHANGED are resolved via the dataset's own canonical key
        columns (never a business-key guess) against the row identities
        already recorded on staging_records for this run — a ROW_INDEX_
        FALLBACK dataset (no reliable key) always reports CHANGED as empty
        and UNCHANGED as everything, since no correction could ever have
        been targeted at a specific physical row in that case (see
        record_builder.parse_record_ref_to_key_dict)."""
        staging_run = self.get_materialized(staging_run_id)
        dataset = self._db.get(Dataset, staging_run.dataset_id)
        key_context = self.resolve_key_context(dataset)

        dest_schema = staging_run.destination_schema
        dest_table = staging_run.destination_table
        qualified_table = f"{quote_pg_identifier(dest_schema)}.{quote_pg_identifier(dest_table)}"

        dest_columns = [
            row[0]
            for row in self._db.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = :table ORDER BY ordinal_position"
                ),
                {"schema": dest_schema, "table": dest_table},
            ).all()
        ]

        staging_records = self._db.execute(
            select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)
        ).scalars().all()
        corrected_fields_by_record_ref = {r.record_ref: r.corrected_fields for r in staging_records}

        changed_key_dicts: list[dict[str, Any]] = []
        for record_ref in corrected_fields_by_record_ref:
            key_dict = parse_record_ref_to_key_dict(
                record_ref, key_strategy=key_context.effective_key_strategy,
                key_column_names=key_context.key_column_names,
            )
            if key_dict is not None:
                changed_key_dicts.append(key_dict)

        where_clause = ""
        params: dict[str, Any] = {}
        if row_filter in ("CHANGED", "UNCHANGED") and key_context.key_column_names:
            if not changed_key_dicts:
                # Nothing is targetable by key — CHANGED is vacuously empty,
                # UNCHANGED is everything (no WHERE clause needed).
                if row_filter == "CHANGED":
                    where_clause = "WHERE FALSE"
            else:
                key_cols_sql = ", ".join(quote_pg_identifier(c) for c in key_context.key_column_names)
                tuple_placeholders = []
                for i, key_dict in enumerate(changed_key_dicts):
                    names = []
                    for j, col in enumerate(key_context.key_column_names):
                        pname = f"k{i}_{j}"
                        params[pname] = key_dict[col]
                        names.append(f":{pname}")
                    tuple_placeholders.append("(" + ", ".join(names) + ")")
                operator = "IN" if row_filter == "CHANGED" else "NOT IN"
                where_clause = f"WHERE ({key_cols_sql}) {operator} ({', '.join(tuple_placeholders)})"
        elif row_filter == "CHANGED":
            # ROW_INDEX_FALLBACK — no key at all, so CHANGED is always empty.
            where_clause = "WHERE FALSE"

        total_rows = self._db.execute(
            text(f"SELECT COUNT(*) FROM {qualified_table} {where_clause}"), params
        ).scalar_one()

        order_columns = key_context.key_column_names or dest_columns
        order_sql = ", ".join(quote_pg_identifier(c) for c in order_columns)
        page_params = {**params, "limit": limit, "offset": offset}
        page_rows = self._db.execute(
            text(f"SELECT * FROM {qualified_table} {where_clause} ORDER BY {order_sql} LIMIT :limit OFFSET :offset"),
            page_params,
        ).mappings().all()

        return MaterializedPreviewResult(
            columns=dest_columns, rows=[dict(r) for r in page_rows], total_rows=total_rows, limit=limit,
            offset=offset, has_more=offset + len(page_rows) < total_rows,
            corrected_fields_by_record_ref=corrected_fields_by_record_ref,
            key_strategy=key_context.effective_key_strategy, key_column_names=key_context.key_column_names,
        )

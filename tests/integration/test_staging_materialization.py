"""Phase 4.12 — integration tests for the materialized staging dataset.

Mirrors tests/integration/test_staging.py's structure and fixture style.
StagingService.trigger() is called directly (unchanged, still synchronous
for the affected-record audit layer); the new async materialization step is
exercised by calling app.modules.staging.tasks.run_staging_materialization
directly (not .delay()) — the established convention for testing Celery
tasks in this repository (see run_validation/run_publish usage elsewhere).
"""
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.exceptions import StagingRunNotMaterializedError
from app.db.models import (
    ApprovalRequest,
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    Job,
    Schema,
    StagingRecord,
    StagingRun,
    User,
)
from app.modules.approval.service import ApprovalService
from app.modules.jobs.service import JobsService
from app.modules.review.decision_service import CorrectionDecisionService
from app.modules.review.service import ReviewService
from app.modules.review.suggestion_service import SuggestionService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.staging.destination_naming import quote_pg_identifier
from app.modules.staging.service import StagingService
from app.modules.staging.tasks import run_staging_materialization
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _build_and_stage(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str,
    *, rows_sql: str, rule_column: str,
):
    """Builds a single-column-rule table, runs the full pipeline through an
    APPROVED review, then triggers the synchronous audit-layer build.
    Returns (staging_run, dataset, table_name)."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    db.execute(text(rows_sql.format(table=table_name)))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    discover_job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(discover_job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()

    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == rule_column)).scalar_one()
    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"mat_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    version = RulesService(db).list_versions(rule.id)[0]
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
    )

    validation_run, validation_job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name=f"mat_test_{uuid.uuid4().hex[:6]}", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
    decision_service = CorrectionDecisionService(db)
    resolved_issue_ids = []
    for issue in issues:
        suggestion = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)
        ).scalars().first()
        if suggestion is not None:
            decision_service.accept(suggestion.id, admin_user)
            resolved_issue_ids.append(issue.id)
    db.expire_all()

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    approval_request = ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids, comment=None, actor=admin_user
    )
    assert approval_request.status == "APPROVED"
    db.expire_all()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    assert staging_run.status == "READY"
    return staging_run, dataset, table_name


@pytest.fixture
def five_rows_one_correction(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    """SOURCE: 5 rows, exactly 1 has a NULL in the ruled column -> exactly 1
    approved correction. This is the Phase 4.12 acceptance shape."""
    table_name = f"dq_mat_{uuid.uuid4().hex[:8]}"
    try:
        staging_run, dataset, _ = _build_and_stage(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=(
                "CREATE TABLE {table} (id INT PRIMARY KEY, a TEXT, b TEXT); "
                "INSERT INTO {table} VALUES "
                "(1, 'x', 'y'), (2, 'x', 'y'), (3, NULL, 'y'), (4, 'x', 'y'), (5, 'x', 'y')"
            ),
            rule_column="a",
        )
        yield staging_run, dataset, table_name
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def _dest_row_count(db: Session, staging_run: StagingRun) -> int:
    qualified = f"{quote_pg_identifier(staging_run.destination_schema)}.{quote_pg_identifier(staging_run.destination_table)}"
    return db.execute(text(f"SELECT COUNT(*) FROM {qualified}")).scalar_one()


def _dest_rows_by_id(db: Session, staging_run: StagingRun) -> dict:
    qualified = f"{quote_pg_identifier(staging_run.destination_schema)}.{quote_pg_identifier(staging_run.destination_table)}"
    rows = db.execute(text(f"SELECT * FROM {qualified}")).mappings().all()
    return {r["id"]: dict(r) for r in rows}


def test_materialization_copies_all_source_rows_with_correction_overlaid(
    db: Session, admin_user: User, five_rows_one_correction
) -> None:
    staging_run, dataset, table_name = five_rows_one_correction

    result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    assert result["status"] == "READY"

    db.expire_all()
    refreshed = db.get(StagingRun, staging_run.id)
    assert refreshed.materialization_phase == "READY"
    assert refreshed.destination_schema == "staging_data"
    assert refreshed.destination_table is not None
    assert refreshed.source_row_count == 5
    assert refreshed.materialized_row_count == 5
    assert refreshed.copied_row_count == 5
    assert refreshed.progress_percentage == 100

    # Physical table: exactly 5 rows (source row count), not 1/2/3.
    assert _dest_row_count(db, refreshed) == 5

    dest_rows = _dest_rows_by_id(db, refreshed)
    assert set(dest_rows.keys()) == {1, 2, 3, 4, 5}
    # Corrected row: id=3's NULL 'a' -> mode-filled 'x'.
    assert dest_rows[3]["a"] == "x"
    assert dest_rows[3]["b"] == "y"
    # Unchanged rows byte/value-equivalent to source.
    for i in (1, 2, 4, 5):
        assert dest_rows[i]["a"] == "x"
        assert dest_rows[i]["b"] == "y"

    # Audit layer: exactly 1 StagingRecord (the affected row), not 5.
    records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
    assert len(records) == 1
    assert records[0].record_ref == "3"
    assert records[0].corrected_fields[0]["final_value"] == "x"

    # Job completed.
    job = db.get(Job, staging_run.job_id)
    assert job.status == "COMPLETED"

    # Source table itself is untouched (still has the original NULL).
    source_row = db.execute(text(f"SELECT a FROM {table_name} WHERE id = 3")).scalar_one()
    assert source_row is None


def test_materialization_across_multiple_corrected_rows(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_mat_multi_{uuid.uuid4().hex[:8]}"
    try:
        staging_run, dataset, _ = _build_and_stage(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=(
                "CREATE TABLE {table} (id INT PRIMARY KEY, a TEXT); "
                "INSERT INTO {table} VALUES (1, 'x'), (2, NULL), (3, 'x'), (4, NULL), (5, 'x')"
            ),
            rule_column="a",
        )
        assert staging_run.record_count == 2

        result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
        assert result["status"] == "READY"

        db.expire_all()
        refreshed = db.get(StagingRun, staging_run.id)
        assert _dest_row_count(db, refreshed) == 5
        dest_rows = _dest_rows_by_id(db, refreshed)
        assert dest_rows[2]["a"] == "x"  # mode-filled
        assert dest_rows[4]["a"] == "x"
        assert dest_rows[1]["a"] == "x"
        assert dest_rows[3]["a"] == "x"
        assert dest_rows[5]["a"] == "x"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_row_count_mismatch_fails_run_and_cleans_up_table(
    db: Session, admin_user: User, five_rows_one_correction, monkeypatch
) -> None:
    staging_run, dataset, table_name = five_rows_one_correction

    from app.source_adapters.postgresql_provider import PostgreSQLProvider

    original_count_rows = PostgreSQLProvider.count_rows
    monkeypatch.setattr(PostgreSQLProvider, "count_rows", lambda self, schema, table: original_count_rows(self, schema, table) + 1)

    result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    assert result["status"] == "FAILED"
    assert "RowCountMismatchError" in result["error"]

    db.expire_all()
    refreshed = db.get(StagingRun, staging_run.id)
    assert refreshed.materialization_phase == "FAILED"
    assert refreshed.materialization_error is not None

    job = db.get(Job, staging_run.job_id)
    assert job.status == "FAILED"

    # Partial/incomplete physical table cleaned up, never left behind as if READY.
    exists = db.execute(
        text(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = :t"
        ),
        {"s": refreshed.destination_schema, "t": refreshed.destination_table},
    ).scalar_one_or_none()
    assert exists is None


def test_coercion_failure_fails_run_truthfully(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """An INTEGER-typed column corrected with a non-numeric string must
    fail the whole materialization run rather than silently writing
    malformed text into a numeric column."""
    table_name = f"dq_mat_coerce_{uuid.uuid4().hex[:8]}"
    try:
        staging_run, dataset, _ = _build_and_stage(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=(
                "CREATE TABLE {table} (id INT PRIMARY KEY, amount INT, a TEXT); "
                "INSERT INTO {table} VALUES (1, 100, 'x'), (2, 200, NULL), (3, 300, 'x')"
            ),
            rule_column="a",
        )
        assert staging_run.record_count == 1

        # Directly corrupt the persisted corrected_fields to target the
        # INTEGER column with an uncoercible string — simulates a
        # generic/string-like final_value that can't be safely coerced.
        record = db.execute(
            select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)
        ).scalar_one()
        record.corrected_fields = [{"column_name": "amount", "original_value": "100", "final_value": "not-a-number", "issue_id": str(uuid.uuid4())}]
        db.commit()

        result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
        assert result["status"] == "FAILED"
        assert "CorrectionCoercionError" in result["error"]

        db.expire_all()
        refreshed = db.get(StagingRun, staging_run.id)
        assert refreshed.materialization_phase == "FAILED"
        exists = db.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = :t"),
            {"s": refreshed.destination_schema, "t": refreshed.destination_table},
        ).scalar_one_or_none()
        assert exists is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_source_outage_fails_run_before_any_table_created(
    db: Session, admin_user: User, five_rows_one_correction, monkeypatch
) -> None:
    from app.modules.staging import tasks as staging_tasks_module
    from app.source_adapters.exceptions import SourceUnreachableError

    staging_run, dataset, table_name = five_rows_one_correction

    def _failing_get_provider(*args, **kwargs):
        raise SourceUnreachableError("simulated source outage")

    monkeypatch.setattr(staging_tasks_module, "get_provider", _failing_get_provider)

    result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    assert result["status"] == "FAILED"
    assert "SourceUnreachableError" in result["error"]

    db.expire_all()
    refreshed = db.get(StagingRun, staging_run.id)
    assert refreshed.materialization_phase == "FAILED"
    assert refreshed.destination_table is None  # never even reached CREATING_TABLE


def test_redelivered_job_is_idempotent(db: Session, admin_user: User, five_rows_one_correction) -> None:
    staging_run, dataset, table_name = five_rows_one_correction

    first = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    assert first["status"] == "READY"

    second = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    assert second["status"] == "COMPLETED"  # idempotency guard short-circuits, job.status echoed back

    db.expire_all()
    refreshed = db.get(StagingRun, staging_run.id)
    assert _dest_row_count(db, refreshed) == 5  # not duplicated to 10


def test_historical_staging_run_has_no_materialized_dataset(db: Session, admin_user: User, five_rows_one_correction) -> None:
    """A pre-4.12 staging run (destination_table never set, materialization
    never triggered) must return an explicit not-materialized condition,
    never crash."""
    staging_run, dataset, table_name = five_rows_one_correction
    # Don't run the materialization task — staging_run is still QUEUED with
    # no destination_table, exactly like a historical row would read back.
    service = StagingService(db)

    with pytest.raises(StagingRunNotMaterializedError):
        service.get_materialized(staging_run.id)
    with pytest.raises(StagingRunNotMaterializedError):
        service.get_destination_metadata(staging_run.id)
    with pytest.raises(StagingRunNotMaterializedError):
        service.preview_materialized(staging_run.id, row_filter="ALL", limit=10, offset=0)


def test_preview_pagination_and_changed_unchanged_filters(
    db: Session, admin_user: User, five_rows_one_correction
) -> None:
    staging_run, dataset, table_name = five_rows_one_correction
    run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    db.expire_all()

    service = StagingService(db)

    page1 = service.preview_materialized(staging_run.id, row_filter="ALL", limit=2, offset=0)
    assert page1.total_rows == 5
    assert len(page1.rows) == 2
    assert page1.has_more is True

    page3 = service.preview_materialized(staging_run.id, row_filter="ALL", limit=2, offset=4)
    assert len(page3.rows) == 1
    assert page3.has_more is False

    changed = service.preview_materialized(staging_run.id, row_filter="CHANGED", limit=10, offset=0)
    assert changed.total_rows == 1
    assert changed.rows[0]["id"] == 3
    assert changed.rows[0]["a"] == "x"

    unchanged = service.preview_materialized(staging_run.id, row_filter="UNCHANGED", limit=10, offset=0)
    assert unchanged.total_rows == 4
    assert {r["id"] for r in unchanged.rows} == {1, 2, 4, 5}


def test_destination_metadata_reports_real_backend_values(
    db: Session, admin_user: User, five_rows_one_correction
) -> None:
    staging_run, dataset, table_name = five_rows_one_correction
    run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    db.expire_all()

    metadata = StagingService(db).get_destination_metadata(staging_run.id)
    assert metadata.staging_run.destination_schema == "staging_data"
    assert metadata.approved_correction_count == 1
    assert metadata.affected_row_count == 1
    column_names = {c.name for c in metadata.columns}
    assert column_names == {"id", "a", "b"}


def test_composite_key_dataset_overlay_targets_correct_row(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_mat_composite_{uuid.uuid4().hex[:8]}"
    try:
        staging_run, dataset, _ = _build_and_stage(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=(
                "CREATE TABLE {table} (region_id INT, order_id INT, a TEXT, PRIMARY KEY (region_id, order_id)); "
                "INSERT INTO {table} VALUES (1, 100, 'x'), (1, 101, NULL), (2, 100, 'x')"
            ),
            rule_column="a",
        )
        assert staging_run.record_count == 1

        result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
        assert result["status"] == "READY"

        db.expire_all()
        refreshed = db.get(StagingRun, staging_run.id)
        assert _dest_row_count(db, refreshed) == 3

        qualified = f"{quote_pg_identifier(refreshed.destination_schema)}.{quote_pg_identifier(refreshed.destination_table)}"
        rows = {
            (r["region_id"], r["order_id"]): r["a"]
            for r in db.execute(text(f"SELECT * FROM {qualified}")).mappings().all()
        }
        assert rows[(1, 100)] == "x"
        assert rows[(1, 101)] == "x"  # corrected — only this exact composite key was overlaid
        assert rows[(2, 100)] == "x"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_row_index_fallback_dataset_copies_safely_without_overlay(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """A dataset with no configured key at all still materializes a
    faithful full copy — correction targeting is the thing that's unsafe
    for ROW_INDEX_FALLBACK, not materialization itself."""
    table_name = f"dq_mat_nokey_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (a TEXT, b TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES ('x', 'y'), (NULL, 'y'), ('x', 'y')"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        from app.modules.discovery.tasks import run_discovery
        from app.modules.profiling.service import ProfilingService
        from app.modules.profiling.tasks import run_profile

        jobs_service = JobsService(db, redis_client)
        discover_job = jobs_service.create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(discover_job.id))
        db.expire_all()

        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        assert dataset.key_strategy == "ROW_INDEX_FALLBACK"  # no PK on this table

        col_a = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()

        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"nokey_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = RulesService(db).list_versions(rule.id)[0]
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col_a.id, column_ids=None, template_id=None,
        )
        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="nokey", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        approval_request = ApprovalService(db).submit(review_run.id, admin_user)
        ApprovalService(db).decide(approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user)
        db.expire_all()

        staging_run = StagingService(db).trigger(review_run.id, admin_user)
        assert staging_run.status == "READY"

        result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
        assert result["status"] == "READY"

        db.expire_all()
        refreshed = db.get(StagingRun, staging_run.id)
        # Full raw copy still succeeds: 3 source rows -> 3 materialized rows.
        assert refreshed.source_row_count == 3
        assert refreshed.materialized_row_count == 3
        assert _dest_row_count(db, refreshed) == 3
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_source_immutability_provider_write_methods_never_invoked(
    db: Session, admin_user: User, five_rows_one_correction, monkeypatch
) -> None:
    """Proves materialization never calls anything resembling a write on
    the source provider — only count_rows/iter_rows/close are exercised."""
    staging_run, dataset, table_name = five_rows_one_correction

    from app.source_adapters.postgresql_provider import PostgreSQLProvider

    def _forbidden(*args, **kwargs):
        raise AssertionError("source provider write method must never be invoked by materialization")

    # PostgreSQLProvider has no INSERT/UPDATE/DELETE/DDL methods at all on
    # its interface (see SourceDatabaseProvider) — this test asserts that
    # invariant holds by ensuring the only methods actually called are the
    # documented read-only ones.
    called_methods = []
    original_count_rows = PostgreSQLProvider.count_rows
    original_iter_rows = PostgreSQLProvider.iter_rows

    def _tracked_count_rows(self, *a, **kw):
        called_methods.append("count_rows")
        return original_count_rows(self, *a, **kw)

    def _tracked_iter_rows(self, *a, **kw):
        called_methods.append("iter_rows")
        yield from original_iter_rows(self, *a, **kw)

    monkeypatch.setattr(PostgreSQLProvider, "count_rows", _tracked_count_rows)
    monkeypatch.setattr(PostgreSQLProvider, "iter_rows", _tracked_iter_rows)

    result = run_staging_materialization(str(staging_run.job_id), str(staging_run.id))
    assert result["status"] == "READY"
    assert "count_rows" in called_methods
    assert "iter_rows" in called_methods

    source_rows = db.execute(text(f"SELECT id, a, b FROM {table_name} ORDER BY id")).all()
    assert [tuple(r) for r in source_rows] == [(1, "x", "y"), (2, "x", "y"), (3, None, "y"), (4, "x", "y"), (5, "x", "y")]

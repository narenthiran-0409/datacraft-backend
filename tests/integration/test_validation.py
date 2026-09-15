"""Integration tests for ValidationService + the run_validation Celery task,
against a real local Postgres table (via pg_connection/db fixtures), mirroring
tests/integration/test_profiling.py's structure and conventions.
"""
import uuid
from unittest.mock import MagicMock

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    Connection,
    Dataset,
    Rule,
    RuleAssignment,
    RuleVersion,
    Schema,
    User,
    ValidationFailure,
    ValidationMetric,
    ValidationResult,
    ValidationRun,
)
from app.modules.jobs.service import JobsService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _make_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, rows_sql: str) -> Dataset:
    from app.modules.discovery.tasks import run_discovery

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT, score NUMERIC)"))
    if rows_sql:
        db.execute(text(rows_sql))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    return db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()


def _assign_rule(
    db: Session, admin_user: User, dataset: Dataset, *, rule_type: str, definition: dict,
    column_id=None, column_ids=None, scope="SINGLE_COLUMN",
) -> RuleAssignment:
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type=rule_type, origin="CUSTOM", definition=definition, severity="HIGH", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    assignment_service = RuleAssignmentService(db)
    return assignment_service.create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope=scope, column_id=column_id, column_ids=column_ids, template_id=None,
    )


def test_zero_row_dataset_completes_without_overwriting_existing_quality_score(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_val_zero_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name, rows_sql="")
        dataset.last_quality_score = 55.00
        db.commit()

        val_column = db.execute(text("SELECT 1")).scalar()  # keep session warm
        assert val_column == 1

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)
        completed_dataset = db.get(Dataset, dataset.id)

        assert completed_run.status == "COMPLETED"
        assert completed_run.total_rows == 0
        assert completed_run.quality_score is None
        assert float(completed_dataset.last_quality_score) == 55.00  # not overwritten
        assert completed_dataset.last_validated_at is not None  # but this IS updated

        metrics = db.execute(
            select(ValidationMetric).where(ValidationMetric.validation_run_id == completed_run.id)
        ).scalars().all()
        by_name = {m.metric_name: float(m.metric_value) for m in metrics}
        assert by_name["total_rows"] == 0
        assert by_name["failure_storage_truncated"] == 0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_quality_score_is_unweighted_pass_rate(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_val_score_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10),(2,'b',20),(3,'c',30),(4,'d',999)",
        )
        from app.db.models import Column

        score_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "score")
        ).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 100}, column_id=score_col.id)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)
        completed_dataset = db.get(Dataset, dataset.id)

        assert completed_run.status == "COMPLETED"
        assert completed_run.total_rows == 4
        assert completed_run.passed_rows == 3
        assert completed_run.failed_rows == 1
        assert float(completed_run.quality_score) == 75.00  # 3/4 * 100, unweighted
        assert float(completed_dataset.last_quality_score) == 75.00
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_failure_cap_truncates_storage_without_altering_aggregate_counts(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_val_cap_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,NULL,1),(2,NULL,2),(3,NULL,3),(4,NULL,4),(5,'x',5)",
        )
        from app.db.models import Column

        val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=val_col.id)

        monkeypatch.setattr(settings, "VALIDATION_MAX_FAILURES", 2)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)

        # 4 rows have a null 'val' -> 4 failing rows in the aggregate, unaffected by the cap.
        assert completed_run.failed_rows == 4
        assert completed_run.total_rows == 5

        persisted_failures = db.execute(
            select(ValidationFailure).where(ValidationFailure.validation_run_id == completed_run.id)
        ).scalars().all()
        assert len(persisted_failures) == 2  # capped

        metrics = {
            m.metric_name: float(m.metric_value)
            for m in db.execute(
                select(ValidationMetric).where(ValidationMetric.validation_run_id == completed_run.id)
            ).scalars()
        }
        assert metrics["failure_storage_truncated"] == 1
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_unsupported_rule_type_is_skipped_and_recorded_as_metric_without_failing_the_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_val_unsupported_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10)",
        )

        # Bypass RulesService (which rejects this at creation time) to prove
        # the engine's own defense-in-depth handling, per the approved plan.
        rule = Rule(name=f"bad_{uuid.uuid4().hex[:8]}", rule_type="REFERENTIAL_INTEGRITY", origin="CUSTOM", status="ACTIVE", created_by=admin_user.id)
        db.add(rule)
        db.flush()
        version = RuleVersion(rule_id=rule.id, version_number=1, definition={}, severity="HIGH", is_current=True, created_by=admin_user.id)
        db.add(version)
        db.flush()
        assignment = RuleAssignment(
            rule_version_id=version.id, dataset_id=dataset.id, assignment_scope="DATASET_LEVEL",
            is_enabled=True, assigned_by=admin_user.id,
        )
        db.add(assignment)
        db.commit()

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)
        assert completed_run.status == "COMPLETED"  # not failed by an unsupported rule type

        metrics = db.execute(
            select(ValidationMetric).where(ValidationMetric.validation_run_id == completed_run.id)
        ).scalars().all()
        unsupported = [m for m in metrics if m.metric_name == "unsupported_rule_type_skipped"]
        assert len(unsupported) == 1
        assert unsupported[0].metric_group == "REFERENTIAL_INTEGRITY"
        assert float(unsupported[0].metric_value) == 1
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_evaluation_timeout_marks_run_and_job_failed(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    from app.modules.validation import tasks as validation_tasks

    table_name = f"dq_val_timeout_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10),(2,'b',20)",
        )
        from app.db.models import Column

        val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
        id_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "id")).scalar_one()
        _assign_rule(
            db, admin_user, dataset, rule_type="CROSS_COLUMN", definition={"check": "all_equal"},
            scope="CROSS_COLUMN", column_ids=[id_col.id, val_col.id],
        )

        monkeypatch.setattr(settings, "VALIDATION_DATASET_TIMEOUT_SECONDS", 0.05)

        def _slow_get_evaluator(rule_type):
            if rule_type == "CROSS_COLUMN":
                import time

                def _slow(**kwargs):
                    time.sleep(1)
                    return {}

                return _slow
            from app.modules.validation.engine import get_evaluator as real_get_evaluator

            return real_get_evaluator(rule_type)

        monkeypatch.setattr(validation_tasks, "get_evaluator", _slow_get_evaluator)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)
        completed_job = JobsService(db, redis_client).get(job.id)

        assert completed_run.status == "FAILED"
        assert completed_job.status == "FAILED"
        assert "timed out" in completed_run.error_message.lower()
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_uncaught_exception_marks_run_and_job_failed_instead_of_a_permanent_zombie(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Regression test for the incident this catch-all exists for: a
    provider whose sample_rows() didn't accept a keyword argument every
    caller passes unconditionally raised an uncaught TypeError, and
    validation_runs/jobs were left stuck at RUNNING forever with no error
    recorded anywhere (neither was ever the specific source_adapters error
    type the task already handled). Simulates that same shape of surprise
    exception and asserts the run/job are marked FAILED with a real
    error_message instead of staying RUNNING."""
    from app.modules.validation import tasks as validation_tasks

    table_name = f"dq_val_uncaught_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10)",
        )

        fake_provider = MagicMock()
        fake_provider.sample_rows.side_effect = TypeError(
            "sample_rows() got an unexpected keyword argument 'row_count_estimate'"
        )
        fake_provider.close.return_value = None
        monkeypatch.setattr(validation_tasks, "get_provider", lambda *a, **kw: fake_provider)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)
        completed_job = JobsService(db, redis_client).get(job.id)

        assert completed_run.status == "FAILED"
        assert completed_run.completed_at is not None
        assert "TypeError" in completed_run.error_message
        assert "row_count_estimate" in completed_run.error_message
        assert completed_job.status == "FAILED"
        assert completed_job.error_message == completed_run.error_message
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_cancel_in_progress_validation(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    from app.modules.validation import tasks as validation_tasks

    table_name = f"dq_val_cancel_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10)",
        )
        from app.db.models import Column

        val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=val_col.id)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        jobs_service = JobsService(db, redis_client)

        fake_provider = MagicMock()
        fake_provider.get_dataset_column_stats.return_value = {}
        fake_provider.sample_rows.return_value = MagicMock(rows=[{"id": 1, "val": "a", "score": 10}], is_full_scan=True)
        fake_provider.close.return_value = None

        def get_provider_and_cancel(*args, **kwargs):
            jobs_service.cancel(job.id)
            return fake_provider

        monkeypatch.setattr(validation_tasks, "get_provider", get_provider_and_cancel)

        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)
        completed_job = jobs_service.get(job.id)
        assert completed_run.status == "CANCELLED"
        assert completed_job.status == "CANCELLED"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_zero_rule_assignments_is_never_indistinguishable_from_a_genuine_all_pass_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """The core regression this feature exists to prevent: a dataset with
    rows but NO enabled RuleAssignments must report rules_evaluated_count=0
    and no_applicable_rules=True, even though every row still trivially
    gets PASSED and quality_score still computes to 100.00 under the
    existing per-row loop. The wire-level signal that nothing was actually
    checked must not depend on the caller re-deriving it from "0 failures
    + 100 score", which is exactly what made the old behavior look like a
    genuine successful validation.
    """
    table_name = f"dq_val_noassign_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10),(2,'b',20)",
        )
        # Deliberately no _assign_rule call — zero enabled RuleAssignments.

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)

        assert completed_run.status == "COMPLETED"
        assert completed_run.total_rows == 2
        assert completed_run.passed_rows == 2
        assert float(completed_run.quality_score) == 100.00  # old ambiguous signal, still true
        # New, unambiguous signal that must accompany it:
        assert completed_run.rules_evaluated_count == 0
        assert completed_run.no_applicable_rules is True

        evaluated = ValidationService(db).list_evaluated_rules(validation_run_id=completed_run.id)
        assert evaluated == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_genuine_all_pass_run_reports_nonzero_rules_evaluated_and_names_them(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """The mirror-image case (Scenario C): rules ARE assigned, all data is
    valid, quality_score is genuinely 100 — and this must be distinguishable
    from the zero-assignment case above via rules_evaluated_count and the
    evaluated-rules detail (rule name + column). Deliberately includes
    COMPLETENESS and UNIQUENESS alongside RANGE — those two exercise the
    exact-stats pushdown path (provider.get_dataset_column_stats), the exact
    code path the NotImplementedError-fallback regression test below
    targets, and all three produce zero failures, so none leave a
    validation_failures row — rules_evaluated_count / list_evaluated_rules
    is the only proof any of them actually ran.
    """
    table_name = f"dq_val_allpass_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10),(2,'b',20),(3,'c',30)",
        )
        from app.db.models import Column

        val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
        score_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "score")
        ).scalar_one()
        range_assignment = _assign_rule(
            db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 100}, column_id=score_col.id
        )
        completeness_assignment = _assign_rule(
            db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=val_col.id
        )
        uniqueness_assignment = _assign_rule(
            db, admin_user, dataset, rule_type="UNIQUENESS", definition={"max_duplicate_percentage": 0}, column_id=val_col.id
        )

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)

        assert completed_run.status == "COMPLETED"
        assert completed_run.total_rows == 3
        assert completed_run.passed_rows == 3
        assert float(completed_run.quality_score) == 100.00
        assert completed_run.rules_evaluated_count == 3
        assert completed_run.no_applicable_rules is False

        evaluated = ValidationService(db).list_evaluated_rules(validation_run_id=completed_run.id)
        assert len(evaluated) == 3
        by_assignment_id = {e["rule_assignment_id"]: e for e in evaluated}
        assert by_assignment_id[range_assignment.id]["rule_type"] == "RANGE"
        assert by_assignment_id[range_assignment.id]["column_name"] == "score"
        assert by_assignment_id[completeness_assignment.id]["rule_type"] == "COMPLETENESS"
        assert by_assignment_id[completeness_assignment.id]["column_name"] == "val"
        assert by_assignment_id[uniqueness_assignment.id]["rule_type"] == "UNIQUENESS"
        assert by_assignment_id[uniqueness_assignment.id]["column_name"] == "val"
        assert all(e["rule_name"] for e in evaluated)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_provider_not_implementing_exact_stats_falls_back_to_sampled_rows_instead_of_failing(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Regression test for a real production incident: SQLServerProvider.
    get_dataset_column_stats() is an unconditional NotImplementedError stub.
    Before this fix, that propagated uncaught past the
    `except ExactStatsTimeoutError` guard, up through _execute_validation's
    try/except (which only catches SourceTimeoutError/SourceQueryError), and
    was only caught by run_validation's outer catch-all — marking the whole
    run FAILED with 0 rows scored, even though sampling itself had already
    succeeded and every assigned rule was otherwise perfectly evaluable from
    the sampled rows alone.

    Provider-agnostic by design: wraps the real PostgreSQLProvider (via the
    real get_provider() factory) so this proves the fix works for ANY
    provider that raises NotImplementedError here, not something specific
    to SQL Server (which isn't exercised by this test suite at all).
    """
    from app.modules.validation import tasks as validation_tasks

    table_name = f"dq_val_noexact_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            rows_sql=f"INSERT INTO {table_name} VALUES (1,'a',10),(2,NULL,20),(3,'c',30)",
        )
        from app.db.models import Column

        val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
        _assign_rule(
            db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=val_col.id
        )

        real_get_provider = validation_tasks.get_provider

        def _get_provider_with_unimplemented_exact_stats(*args, **kwargs):
            provider = real_get_provider(*args, **kwargs)

            def _raise_not_implemented(*_args, **_kwargs):
                raise NotImplementedError("Implemented in a later phase")

            provider.get_dataset_column_stats = _raise_not_implemented
            return provider

        monkeypatch.setattr(validation_tasks, "get_provider", _get_provider_with_unimplemented_exact_stats)

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))

        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)

        # The key assertion: NOT FAILED. Sampled-row fallback still finds the
        # real NULL at row_index=1 — this is genuine evaluation, not a run
        # that merely avoided crashing.
        assert completed_run.status == "COMPLETED"
        assert completed_run.total_rows == 3
        assert completed_run.passed_rows == 2
        assert completed_run.failed_rows == 1
        assert completed_run.rules_evaluated_count == 1
        assert completed_run.no_applicable_rules is False

        failures = db.execute(
            select(ValidationFailure).where(ValidationFailure.validation_run_id == completed_run.id)
        ).scalars().all()
        assert len(failures) == 1
        assert "null" in (failures[0].reason or "").lower()
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

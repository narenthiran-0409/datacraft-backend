"""Integration tests for Phase 11 (Reports) against real local Postgres,
using the actual pipeline services (not direct ORM construction) so the
data reports read is produced the exact same way production data is.
Every assertion compares against an INDEPENDENTLY computed expected value
— never merely a 200/shape check.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    ApprovalRequest,
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    RuleVersion,
    Schema,
    User,
    ValidationRun,
)
from app.modules.reports.service import ReportsService


def _build_validated_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, values: list):
    """Discovers + profiles + validates a table with a COMPLETENESS rule on
    'val'. `values` controls which rows are NULL (fail) vs not (pass)."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    value_sql = ", ".join(f"({i+1}, {('NULL' if v is None else repr(v))})" for i, v in enumerate(values))
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES {value_sql}"))
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
    val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"rpt_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=val_col.id, column_ids=None, template_id=None,
    )

    validation_run, validation_job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()
    return dataset, db.get(ValidationRun, validation_run.id)


def test_quality_trend_returns_correct_historical_scores(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_rpt_trend_{uuid.uuid4().hex[:8]}"
    try:
        dataset, run = _build_validated_dataset(db, redis_client, admin_user, pg_connection, table_name, ["a", "a", None, "a"])
        expected_score = Decimal("75.00")  # 3 passed / 4 total
        assert run.quality_score == expected_score  # sanity on the independently-known Phase 5 formula

        points = ReportsService(db).get_quality_trend(
            dataset_id=dataset.id, from_dt=run.created_at - timedelta(hours=1), to_dt=run.created_at + timedelta(hours=1)
        )
        assert len(points) == 1
        assert points[0]["quality_score"] == expected_score
        assert points[0]["validation_run_id"] == run.id
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_quality_trend_excludes_null_score_zero_row_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    table_name = f"dq_rpt_zero_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        job = JobsService(db, redis_client).create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()
        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"rpt_zero_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
        )
        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()
        run = db.get(ValidationRun, validation_run.id)
        assert run.quality_score is None  # confirmed zero-row Phase 5 behavior

        points = ReportsService(db).get_quality_trend(
            dataset_id=dataset.id, from_dt=run.created_at - timedelta(hours=1), to_dt=run.created_at + timedelta(hours=1)
        )
        assert points == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_quality_by_dataset_uses_latest_score(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    table_name = f"dq_rpt_latest_{uuid.uuid4().hex[:8]}"
    try:
        dataset, first_run = _build_validated_dataset(db, redis_client, admin_user, pg_connection, table_name, ["a", None])
        assert first_run.quality_score == Decimal("50.00")

        db.execute(text(f"UPDATE {table_name} SET val = 'a' WHERE id = 2"))
        db.commit()
        second_run, second_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(second_job.id), str(second_run.id))
        db.expire_all()
        second_run = db.get(ValidationRun, second_run.id)
        assert second_run.quality_score == Decimal("100.00")

        results = ReportsService(db).get_quality_by_dataset(data_source_id=None)
        row = next(r for r in results if r["dataset_id"] == dataset.id)
        assert row["latest_quality_score"] == Decimal("100.00")
        assert row["latest_validation_run_id"] == second_run.id
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_rule_effectiveness_correct_counts_and_no_raw_values(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_rpt_rule_{uuid.uuid4().hex[:8]}"
    try:
        dataset, run = _build_validated_dataset(db, redis_client, admin_user, pg_connection, table_name, ["a", None, None, "a"])
        expected_failure_count = 2  # two NULLs

        rows = ReportsService(db).get_rule_effectiveness(
            dataset_id=dataset.id, from_dt=run.created_at - timedelta(hours=1), to_dt=run.created_at + timedelta(hours=1)
        )
        assert len(rows) == 1
        assert rows[0]["failure_count"] == expected_failure_count
        assert rows[0]["failure_rate"] == Decimal("1")  # only rule in scope -> 100% of failures
        assert rows[0]["severity_breakdown"] == {"HIGH": 2}

        # Structural + payload-level: never expose failed_value/original_value.
        assert "failed_value" not in rows[0]
        assert "original_value" not in rows[0]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_review_performance_correct_throughput_and_latency(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService

    table_name = f"dq_rpt_review_{uuid.uuid4().hex[:8]}"
    try:
        dataset, run = _build_validated_dataset(db, redis_client, admin_user, pg_connection, table_name, ["a", "a", None])
        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=run.id, name="rpt_review", actor=admin_user
        )
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        before_decide = datetime.now(timezone.utc)
        correction = CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        rows = ReportsService(db).get_review_performance(
            from_dt=before_decide - timedelta(hours=1), to_dt=before_decide + timedelta(hours=1)
        )
        row = next(r for r in rows if r["reviewer_id"] == admin_user.id)
        assert row["decision_count"] == 1
        expected_latency = (correction.decided_at - issue.created_at).total_seconds()
        assert abs(row["avg_latency_seconds"] - expected_latency) < 1.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_approval_metrics_correct_rates_and_latency(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.approval.service import ApprovalService
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService

    table_name = f"dq_rpt_approval_{uuid.uuid4().hex[:8]}"
    try:
        dataset, run = _build_validated_dataset(db, redis_client, admin_user, pg_connection, table_name, ["a", "a", None])
        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=run.id, name="rpt_approval", actor=admin_user
        )
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        approval_request = ApprovalService(db).submit(review_run.id, admin_user)
        approval_request = ApprovalService(db).decide(
            approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user
        )
        db.expire_all()
        approval_request = db.get(ApprovalRequest, approval_request.id)

        metrics = ReportsService(db).get_approval_metrics(
            from_dt=approval_request.requested_at - timedelta(hours=1), to_dt=approval_request.requested_at + timedelta(hours=1)
        )
        assert metrics["total_requests"] == 1
        assert metrics["approved_count"] == 1
        assert metrics["approval_rate"] == Decimal("1")
        expected_latency = (approval_request.decided_at - approval_request.requested_at).total_seconds()
        assert abs(metrics["avg_decision_latency_seconds"] - expected_latency) < 1.0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_dataset_with_no_validation_runs_returns_empty_not_error(db: Session, pg_connection: Connection) -> None:
    now = datetime.now(timezone.utc)
    points = ReportsService(db).get_quality_trend(dataset_id=uuid.uuid4(), from_dt=now - timedelta(days=1), to_dt=now)
    assert points == []

"""Unit-adjacent tests for ReportsService's aggregation/calculation logic,
against real local Postgres (mirroring test_lineage_service.py's
precedent) but with rows constructed DIRECTLY via ORM inserts — bypassing
the full pipeline entirely — to isolate the arithmetic being tested from
any other module's behavior.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import (
    ApprovalRequest,
    Correction,
    Dataset,
    Issue,
    ReviewRun,
    Rule,
    RuleAssignment,
    RuleVersion,
    Schema,
    User,
    ValidationFailure,
    ValidationResult,
    ValidationRun,
)
from app.modules.reports.service import ReportsService


def _make_dataset(db: Session, connection_id: uuid.UUID, name: str | None = None) -> Dataset:
    schema = Schema(connection_id=connection_id, name=f"sch_{uuid.uuid4().hex[:8]}")
    db.add(schema)
    db.flush()
    dataset = Dataset(schema_id=schema.id, name=name or f"ds_{uuid.uuid4().hex[:8]}")
    db.add(dataset)
    db.flush()
    return dataset


def _make_validation_run(db: Session, dataset_id: uuid.UUID, *, quality_score, created_at) -> ValidationRun:
    run = ValidationRun(
        dataset_id=dataset_id, status="COMPLETED", total_rows=10, passed_rows=8, warning_rows=0, failed_rows=2,
        quality_score=quality_score, created_at=created_at,
    )
    db.add(run)
    db.flush()
    return run


# --- quality-trend -----------------------------------------------------


def test_quality_trend_excludes_null_quality_score(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    _make_validation_run(db, dataset.id, quality_score=Decimal("80.00"), created_at=now - timedelta(days=1))
    _make_validation_run(db, dataset.id, quality_score=None, created_at=now)  # zero-row run, must be excluded
    db.commit()

    points = ReportsService(db).get_quality_trend(
        dataset_id=dataset.id, from_dt=now - timedelta(days=2), to_dt=now + timedelta(days=1)
    )
    assert len(points) == 1
    assert points[0]["quality_score"] == Decimal("80.00")


def test_quality_trend_orders_by_created_at(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    run_later = _make_validation_run(db, dataset.id, quality_score=Decimal("90.00"), created_at=now)
    run_earlier = _make_validation_run(db, dataset.id, quality_score=Decimal("70.00"), created_at=now - timedelta(days=5))
    db.commit()

    points = ReportsService(db).get_quality_trend(
        dataset_id=dataset.id, from_dt=now - timedelta(days=10), to_dt=now + timedelta(days=1)
    )
    assert [p["validation_run_id"] for p in points] == [run_earlier.id, run_later.id]


def test_quality_trend_respects_date_range(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    _make_validation_run(db, dataset.id, quality_score=Decimal("50.00"), created_at=now - timedelta(days=30))
    db.commit()

    points = ReportsService(db).get_quality_trend(
        dataset_id=dataset.id, from_dt=now - timedelta(days=5), to_dt=now
    )
    assert points == []


def test_quality_trend_empty_dataset_returns_empty_list(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    points = ReportsService(db).get_quality_trend(dataset_id=dataset.id, from_dt=now - timedelta(days=1), to_dt=now)
    assert points == []


# --- quality-by-dataset --------------------------------------------------


def test_quality_by_dataset_uses_latest_non_null_score(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    _make_validation_run(db, dataset.id, quality_score=Decimal("60.00"), created_at=now - timedelta(days=2))
    latest = _make_validation_run(db, dataset.id, quality_score=Decimal("95.00"), created_at=now)
    _make_validation_run(db, dataset.id, quality_score=None, created_at=now + timedelta(days=1))  # newer but NULL, must not win
    db.commit()

    results = ReportsService(db).get_quality_by_dataset(data_source_id=None)
    row = next(r for r in results if r["dataset_id"] == dataset.id)
    assert row["latest_quality_score"] == Decimal("95.00")
    assert row["latest_validation_run_id"] == latest.id


def test_quality_by_dataset_no_validation_runs_returns_zero_not_error(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    db.commit()

    results = ReportsService(db).get_quality_by_dataset(data_source_id=None)
    row = next(r for r in results if r["dataset_id"] == dataset.id)
    assert row["latest_quality_score"] is None
    assert row["latest_validation_run_id"] is None


def test_quality_by_dataset_only_null_score_runs_does_not_fabricate_score(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    _make_validation_run(db, dataset.id, quality_score=None, created_at=now)
    db.commit()

    results = ReportsService(db).get_quality_by_dataset(data_source_id=None)
    row = next(r for r in results if r["dataset_id"] == dataset.id)
    assert row["latest_quality_score"] is None


# --- rule-effectiveness ----------------------------------------------------


def _make_rule_assignment(db: Session, dataset_id: uuid.UUID, *, severity_definition="HIGH") -> RuleAssignment:
    rule = Rule(name=f"rule_{uuid.uuid4().hex[:8]}", rule_type="COMPLETENESS")
    db.add(rule)
    db.flush()
    version = RuleVersion(rule_id=rule.id, version_number=1, definition={"max_null_percentage": 0}, severity=severity_definition)
    db.add(version)
    db.flush()
    assignment = RuleAssignment(rule_version_id=version.id, dataset_id=dataset_id, assignment_scope="DATASET_LEVEL")
    db.add(assignment)
    db.flush()
    return assignment


def _make_failure(db: Session, run: ValidationRun, assignment: RuleAssignment, *, severity: str, row_index: int) -> ValidationFailure:
    result = ValidationResult(
        validation_run_id=run.id, record_ref=str(row_index), row_index=row_index, status="FAILED", source_row_hash="h",
    )
    db.add(result)
    db.flush()
    failure = ValidationFailure(
        validation_result_id=result.id, validation_run_id=run.id, rule_assignment_id=assignment.id, severity=severity,
    )
    db.add(failure)
    db.flush()
    return failure


def test_rule_effectiveness_counts_and_rate(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    run = _make_validation_run(db, dataset.id, quality_score=Decimal("50.00"), created_at=now)
    assignment_a = _make_rule_assignment(db, dataset.id)
    assignment_b = _make_rule_assignment(db, dataset.id)
    _make_failure(db, run, assignment_a, severity="HIGH", row_index=1)
    _make_failure(db, run, assignment_a, severity="HIGH", row_index=2)
    _make_failure(db, run, assignment_b, severity="MEDIUM", row_index=3)
    db.commit()

    rows = ReportsService(db).get_rule_effectiveness(
        dataset_id=dataset.id, from_dt=now - timedelta(hours=1), to_dt=now + timedelta(hours=1)
    )
    assert sum(r["failure_count"] for r in rows) == 3
    assert any(r["failure_count"] == 2 and r["failure_rate"] == Decimal("2") / Decimal("3") for r in rows)
    assert any(r["failure_count"] == 1 and r["failure_rate"] == Decimal("1") / Decimal("3") for r in rows)


def test_rule_effectiveness_severity_breakdown(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    run = _make_validation_run(db, dataset.id, quality_score=Decimal("50.00"), created_at=now)
    assignment = _make_rule_assignment(db, dataset.id)
    _make_failure(db, run, assignment, severity="HIGH", row_index=1)
    _make_failure(db, run, assignment, severity="LOW", row_index=2)
    db.commit()

    rows = ReportsService(db).get_rule_effectiveness(
        dataset_id=dataset.id, from_dt=now - timedelta(hours=1), to_dt=now + timedelta(hours=1)
    )
    assert len(rows) == 1
    assert rows[0]["severity_breakdown"] == {"HIGH": 1, "LOW": 1}


def test_rule_effectiveness_empty_range_returns_empty_list(db: Session, pg_connection) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    rows = ReportsService(db).get_rule_effectiveness(dataset_id=dataset.id, from_dt=now, to_dt=now)
    assert rows == []


def test_rule_effectiveness_response_never_exposes_raw_values(db: Session, pg_connection) -> None:
    from app.modules.reports.schemas import RuleEffectivenessRow
    assert "failed_value" not in RuleEffectivenessRow.model_fields
    assert "original_value" not in RuleEffectivenessRow.model_fields
    assert "final_value" not in RuleEffectivenessRow.model_fields


# --- review-performance ----------------------------------------------------


def _make_issue_and_correction(db: Session, review_run_id: uuid.UUID, validation_failure_id: uuid.UUID, *, decided_by, issue_created_at, decided_at, status="ACCEPTED") -> Correction:
    issue = Issue(
        review_run_id=review_run_id, validation_failure_id=validation_failure_id, record_ref="1", row_index=1,
        severity="HIGH", status="RESOLVED", created_at=issue_created_at,
    )
    db.add(issue)
    db.flush()
    correction = Correction(
        issue_id=issue.id, final_value="x", status=status, decided_by=decided_by, decided_at=decided_at,
    )
    db.add(correction)
    db.flush()
    return correction


def test_review_performance_throughput_and_latency(db: Session, pg_connection, admin_user: User) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    run = _make_validation_run(db, dataset.id, quality_score=Decimal("50.00"), created_at=now)
    assignment = _make_rule_assignment(db, dataset.id)
    failure_1 = _make_failure(db, run, assignment, severity="HIGH", row_index=1)
    failure_2 = _make_failure(db, run, assignment, severity="HIGH", row_index=2)
    review_run = ReviewRun(validation_run_id=run.id, status="DRAFT")
    db.add(review_run)
    db.flush()

    issue_created = now - timedelta(hours=2)
    _make_issue_and_correction(
        db, review_run.id, failure_1.id, decided_by=admin_user.id, issue_created_at=issue_created, decided_at=issue_created + timedelta(hours=1)
    )
    _make_issue_and_correction(
        db, review_run.id, failure_2.id, decided_by=admin_user.id, issue_created_at=issue_created, decided_at=issue_created + timedelta(hours=3)
    )
    db.commit()

    rows = ReportsService(db).get_review_performance(from_dt=now - timedelta(days=1), to_dt=now + timedelta(days=1))
    row = next(r for r in rows if r["reviewer_id"] == admin_user.id)
    assert row["decision_count"] == 2
    assert row["avg_latency_seconds"] == (3600 + 10800) / 2


def test_review_performance_empty_range_returns_empty_list(db: Session) -> None:
    now = datetime.now(timezone.utc)
    rows = ReportsService(db).get_review_performance(from_dt=now, to_dt=now)
    assert rows == []


# --- approval-metrics -------------------------------------------------------


def test_approval_metrics_counts_rate_and_latency(db: Session, pg_connection, admin_user: User) -> None:
    dataset = _make_dataset(db, pg_connection.id)
    now = datetime.now(timezone.utc)
    run = _make_validation_run(db, dataset.id, quality_score=Decimal("50.00"), created_at=now)
    review_run = ReviewRun(validation_run_id=run.id, status="DRAFT")
    db.add(review_run)
    db.flush()

    req_1 = ApprovalRequest(
        review_run_id=review_run.id, status="APPROVED", affected_issue_count=1, affected_record_count=1,
        requested_at=now - timedelta(hours=2), decided_at=now - timedelta(hours=1),
    )
    req_2 = ApprovalRequest(
        review_run_id=review_run.id, status="REJECTED", affected_issue_count=1, affected_record_count=1,
        requested_at=now - timedelta(hours=2), decided_at=now,
    )
    req_3 = ApprovalRequest(
        review_run_id=review_run.id, status="PENDING", affected_issue_count=1, affected_record_count=1,
        requested_at=now - timedelta(hours=1), decided_at=None,
    )
    db.add_all([req_1, req_2, req_3])
    db.commit()

    metrics = ReportsService(db).get_approval_metrics(from_dt=now - timedelta(days=1), to_dt=now + timedelta(days=1))
    assert metrics["total_requests"] == 3
    assert metrics["approved_count"] == 1
    assert metrics["rejected_count"] == 1
    assert metrics["pending_count"] == 1
    assert metrics["approval_rate"] == Decimal("1") / Decimal("3")
    assert metrics["avg_decision_latency_seconds"] == (3600 + 7200) / 2


def test_approval_metrics_zero_requests_no_division_by_zero(db: Session) -> None:
    now = datetime.now(timezone.utc)
    metrics = ReportsService(db).get_approval_metrics(from_dt=now, to_dt=now)
    assert metrics["total_requests"] == 0
    assert metrics["approval_rate"] is None
    assert metrics["avg_decision_latency_seconds"] is None

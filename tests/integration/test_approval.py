"""Integration tests for Phase 7 (Approval) against a real local Postgres
instance, mirroring tests/integration/test_review.py's structure and
conventions.
"""
import threading
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.exceptions import (
    ApprovalRequestAlreadyPendingError,
    InvalidReviewStatusTransitionError,
    IssueNotInApprovalScopeError,
    NoResolvedIssuesError,
    ReviewRunNotReadyForSubmissionError,
)
from app.db.models import (
    ApprovalDecision,
    ApprovalDecisionIssue,
    ApprovalRequest,
    Column,
    Connection,
    Dataset,
    Issue,
    ReviewRun,
    Schema,
    User,
)
from app.modules.approval.service import ApprovalService
from app.modules.jobs.service import JobsService
from app.modules.review.decision_service import CorrectionDecisionService
from app.modules.review.service import ReviewService
from app.modules.review.suggestion_service import SuggestionService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _build_review_run_with_n_resolved_issues(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, n: int
) -> tuple[ReviewRun, list[uuid.UUID]]:
    """Builds a dataset with n columns (col_0..col_{n-1}), each with exactly
    one NULL row and a COMPLETENESS rule, runs discovery -> profiling ->
    validation -> review -> generate-suggestions -> accepts every
    resulting suggestion, giving n resolved issues."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    col_names = [f"col_{i}" for i in range(n)]
    col_defs = ", ".join(f"{c} TEXT" for c in col_names)
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, {col_defs})"))

    # 3 rows per column: 2 non-null (to give mode_fill something to work
    # with) + 1 null, staggered so each column's null lands on a different id.
    rows = []
    for row_id in range(1, n + 2):
        values = []
        for col_idx in range(n):
            values.append("NULL" if row_id == col_idx + 1 else f"'v{col_idx}'")
        rows.append(f"({row_id}, {', '.join(values)})")
    db.execute(text(f"INSERT INTO {table_name} VALUES {', '.join(rows)}"))
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

    rules_service = RulesService(db)
    assignment_service = RuleAssignmentService(db)
    for col_name in col_names:
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == col_name)).scalar_one()
        rule = rules_service.create_rule(
            actor=admin_user, name=f"{col_name}_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = rules_service.list_versions(rule.id)[0]
        assignment_service.create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
        )

    validation_run, validation_job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name=f"approval_test_{uuid.uuid4().hex[:6]}", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
    decision_service = CorrectionDecisionService(db)
    resolved_issue_ids = []
    from app.db.models import CorrectionSuggestion

    for issue in issues:
        suggestion = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)
        ).scalars().first()
        if suggestion is not None:
            decision_service.accept(suggestion.id, admin_user)
            resolved_issue_ids.append(issue.id)

    db.expire_all()
    return db.get(ReviewRun, review_run.id), resolved_issue_ids


@pytest.fixture
def review_with_4_resolved_issues(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    table_name = f"dq_approval_{uuid.uuid4().hex[:8]}"
    try:
        review_run, resolved_issue_ids = _build_review_run_with_n_resolved_issues(
            db, redis_client, admin_user, pg_connection, table_name, n=4
        )
        assert len(resolved_issue_ids) == 4  # sanity check on the fixture itself
        yield review_run, resolved_issue_ids
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_submit_with_zero_resolved_issues_returns_409_and_creates_no_row(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_approval_zero_{uuid.uuid4().hex[:8]}"
    try:
        review_run, resolved_issue_ids = _build_review_run_with_n_resolved_issues(
            db, redis_client, admin_user, pg_connection, table_name, n=1
        )
        # Un-resolve the one issue by not accepting anything — build fresh
        # instead, skipping the accept step this time.
        db.execute(text("DELETE FROM corrections WHERE issue_id IN (SELECT id FROM issues WHERE review_run_id = :rid)"), {"rid": review_run.id})
        db.execute(text("UPDATE issues SET status = 'PENDING' WHERE review_run_id = :rid"), {"rid": review_run.id})
        db.commit()

        before_count = db.execute(select(ApprovalRequest)).scalars().all()
        with pytest.raises(NoResolvedIssuesError):
            ApprovalService(db).submit(review_run.id, admin_user)
        after_count = db.execute(select(ApprovalRequest)).scalars().all()
        assert len(after_count) == len(before_count)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_submit_while_not_in_review_returns_409(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, _ = review_with_4_resolved_issues
    review_run.status = "DRAFT"
    db.commit()

    with pytest.raises(ReviewRunNotReadyForSubmissionError):
        ApprovalService(db).submit(review_run.id, admin_user)


def test_submit_succeeds_and_computes_counts_correctly(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)

    assert approval_request.status == "PENDING"
    assert approval_request.affected_issue_count == 4
    assert approval_request.affected_record_count == 4  # 4 distinct rows, one issue each

    db.expire_all()
    assert db.get(ReviewRun, review_run.id).status == "READY_FOR_APPROVAL"


def test_submit_while_pending_request_exists_returns_409(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, _ = review_with_4_resolved_issues
    ApprovalService(db).submit(review_run.id, admin_user)

    # Move the review run back to IN_REVIEW to isolate this check from the
    # "not IN_REVIEW" guard (real Phase 6 flow would never do this — direct
    # ORM manipulation is only to isolate this one precondition).
    db.execute(text("UPDATE review_runs SET status = 'IN_REVIEW' WHERE id = :rid"), {"rid": review_run.id})
    db.commit()
    db.expire_all()  # the ORM identity map won't see the raw SQL update otherwise

    with pytest.raises(ApprovalRequestAlreadyPendingError):
        ApprovalService(db).submit(review_run.id, admin_user)


def test_correction_decision_guard_blocks_all_six_operations_after_submission(
    db: Session, admin_user: User, review_with_4_resolved_issues
) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    ApprovalService(db).submit(review_run.id, admin_user)
    db.expire_all()

    decision_service = CorrectionDecisionService(db)
    from app.db.models import Correction, CorrectionSuggestion

    issue_id = resolved_issue_ids[0]
    correction = db.execute(select(Correction).where(Correction.issue_id == issue_id)).scalar_one()
    suggestion_id = correction.correction_suggestion_id

    with pytest.raises(InvalidReviewStatusTransitionError):
        decision_service.accept(suggestion_id, admin_user)
    with pytest.raises(InvalidReviewStatusTransitionError):
        decision_service.edit(suggestion_id, "manual", admin_user)
    with pytest.raises(InvalidReviewStatusTransitionError):
        decision_service.reject(suggestion_id, None, admin_user)
    with pytest.raises(InvalidReviewStatusTransitionError):
        decision_service.skip(issue_id, admin_user)
    with pytest.raises(InvalidReviewStatusTransitionError):
        decision_service.correct_directly(issue_id, "manual", admin_user)
    with pytest.raises(InvalidReviewStatusTransitionError):
        decision_service.bulk_action(review_run_id=review_run.id, issue_ids=[issue_id], action="skip", actor=admin_user)


def test_archive_guard_blocks_archiving_with_pending_approval_request(
    db: Session, admin_user: User, review_with_4_resolved_issues
) -> None:
    review_run, _ = review_with_4_resolved_issues
    ApprovalService(db).submit(review_run.id, admin_user)
    db.expire_all()

    with pytest.raises(InvalidReviewStatusTransitionError):
        ReviewService(db).archive(review_run.id, admin_user)


def test_full_approve_flow_single_decide_call(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)

    result = ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids, comment="looks good", actor=admin_user
    )
    assert result.status == "APPROVED"
    assert result.decided_at is not None


def test_full_reject_flow(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)

    result = ApprovalService(db).decide(
        approval_request.id, decision="REJECT", issue_ids=resolved_issue_ids, comment=None, actor=admin_user
    )
    assert result.status == "REJECTED"
    assert result.decided_at is not None


def test_partial_flow_across_two_decide_calls(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    service = ApprovalService(db)

    first = service.decide(
        approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids[:2], comment=None, actor=admin_user
    )
    assert first.status == "PARTIALLY_APPROVED"
    assert first.decided_at is None

    second = service.decide(
        approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids[2:], comment=None, actor=admin_user
    )
    assert second.status == "APPROVED"
    assert second.decided_at is not None


def test_mixed_outcome_resolves_to_rejected(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    service = ApprovalService(db)

    service.decide(approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids[:1], comment=None, actor=admin_user)
    final = service.decide(
        approval_request.id, decision="REJECT", issue_ids=resolved_issue_ids[1:], comment=None, actor=admin_user
    )
    assert final.status == "REJECTED"  # one REJECT among the decisions -> REJECTED overall


def test_deciding_already_decided_issue_returns_409(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    service = ApprovalService(db)

    service.decide(approval_request.id, decision="APPROVE", issue_ids=[resolved_issue_ids[0]], comment=None, actor=admin_user)

    with pytest.raises(IssueNotInApprovalScopeError):
        service.decide(approval_request.id, decision="APPROVE", issue_ids=[resolved_issue_ids[0]], comment=None, actor=admin_user)


def test_get_detail_computes_decided_and_remaining_counts(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    service = ApprovalService(db)

    service.decide(approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids[:1], comment=None, actor=admin_user)

    ar, decided_count, remaining_count = service.get_detail(approval_request.id)
    assert decided_count == 1
    assert remaining_count == 3

    # NEITHER count is a persisted column.
    from app.core.database import engine
    with engine.connect() as conn:
        cols = {row[0] for row in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'approval_requests'"
        ))}
    assert "decided_count" not in cols
    assert "remaining_count" not in cols


def test_audit_events_contain_no_raw_issue_values(db: Session, admin_user: User, review_with_4_resolved_issues) -> None:
    from app.db.models import AuditEvent

    review_run, resolved_issue_ids = review_with_4_resolved_issues
    # Sentinel value planted via the fixture's mode_fill suggestions —
    # confirm none of the actual accepted final_values leak into approval
    # audit events.
    from app.db.models import Correction

    final_values = [
        c.final_value for c in db.execute(select(Correction).where(Correction.issue_id.in_(resolved_issue_ids))).scalars()
    ]

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids, comment="all good", actor=admin_user
    )

    events = db.execute(
        select(AuditEvent).where(AuditEvent.action.in_(("approval_request.submitted", "approval_request.decided")))
    ).scalars().all()
    assert len(events) == 2
    for event in events:
        blob = str(event.before_value) + str(event.after_value) + str(event.audit_metadata)
        for value in final_values:
            if value:
                assert value not in blob


def test_concurrent_decide_calls_on_same_issue_row_lock_prevents_double_decision(
    db: Session, admin_user: User, review_with_4_resolved_issues
) -> None:
    """Genuine concurrency test: two real threads, each with its own DB
    session/connection, both call decide() targeting the SAME single issue
    at (as close to) the same instant, via a threading.Barrier. Without the
    SELECT ... FOR UPDATE row lock, both could read "issue is undecided"
    under READ COMMITTED and both successfully record a decision. With the
    lock, one blocks until the other commits, then correctly sees the issue
    as already decided and raises IssueNotInApprovalScopeError."""
    review_run, resolved_issue_ids = review_with_4_resolved_issues
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    db.commit()

    target_issue_id = resolved_issue_ids[0]
    admin_user_id = admin_user.id
    approval_request_id = approval_request.id

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _worker(name: str) -> None:
        thread_db = SessionLocal()
        try:
            thread_user = thread_db.get(User, admin_user_id)
            service = ApprovalService(thread_db)
            barrier.wait(timeout=10)
            try:
                outcome = service.decide(
                    approval_request_id, decision="APPROVE", issue_ids=[target_issue_id], comment=None,
                    actor=thread_user,
                )
                results[name] = ("success", outcome.status)
            except IssueNotInApprovalScopeError as exc:
                results[name] = ("blocked", str(exc))
        finally:
            thread_db.close()

    t1 = threading.Thread(target=_worker, args=("t1",))
    t2 = threading.Thread(target=_worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert "t1" in results and "t2" in results
    outcomes = [results["t1"], results["t2"]]
    successes = [o for o in outcomes if o[0] == "success"]
    blocked = [o for o in outcomes if o[0] == "blocked"]

    # Exactly one thread succeeded in deciding the issue; the other correctly
    # saw it as already-decided (not corrupted/duplicated state).
    assert len(successes) == 1
    assert len(blocked) == 1

    db.expire_all()
    decision_rows_for_issue = db.execute(
        select(ApprovalDecisionIssue).where(ApprovalDecisionIssue.issue_id == target_issue_id)
    ).scalars().all()
    assert len(decision_rows_for_issue) == 1  # exactly one decision recorded, never two

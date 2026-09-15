"""Phase 4.8 integration tests, run against this project's own local
Postgres database (via the pg_connection fixture) — mirrors
tests/integration/test_staging.py's own established workflow helper,
customized per rule type. Every scenario goes through the REAL
Draft -> IN_REVIEW -> decision -> READY_FOR_APPROVAL -> APPROVED ->
staging workflow; nothing here bypasses approval.
"""
import inspect
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import (
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    ReviewRun,
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
from app.modules.staging.revalidation_service import StagedRevalidationService
from app.modules.staging.service import StagingService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation
from app.modules.validation.staged_revalidation import (
    STATUS_REQUIRES_DATASET_REVALIDATION,
    STATUS_REVALIDATED_FAIL,
    STATUS_REVALIDATED_PASS,
)


def _discover(db: Session, redis_client, connection: Connection, actor: User) -> None:
    from app.modules.discovery.tasks import run_discovery

    job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=connection.id, created_by=actor.id
    )
    run_discovery(str(job.id))
    db.expire_all()


def _assign_rule(db, admin_user, dataset, *, rule_type, definition, column_id, is_enabled=True):
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"p48_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type=rule_type, origin="CUSTOM", definition=definition, severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    assignment = RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=column_id, column_ids=None, template_id=None,
    )
    if not is_enabled:
        assignment.is_enabled = False
        db.commit()
    return assignment


def _setup(db, redis_client, admin_user, pg_connection, table_name, ddl, rows_sql):
    db.execute(text(ddl.format(table=table_name)))
    db.execute(text(rows_sql.format(table=table_name)))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()
    _discover(db, redis_client, pg_connection, admin_user)
    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    return dataset


def _validate_and_review(db, admin_user, dataset, name):
    """Creates the review run (DRAFT) from a real validation run. Stays
    DRAFT deliberately — each test inserts its own controlled
    CorrectionSuggestion(s) for the specific issue(s) it cares about,
    THEN calls _transition_to_review (below), which performs the real
    DRAFT -> IN_REVIEW transition via SuggestionService.generate_for_review_run
    — the one real API path that does it. Because the test's own
    suggestion(s) already exist by then, generate_for_review_run's own
    target_issues filter (skip any issue that already has one) never
    creates a competing suggestion for them."""
    validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()
    review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name=name, actor=admin_user)
    db.expire_all()
    return review_run


def _transition_to_review(db, admin_user, review_run):
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()


def _make_suggestion(db, issue_id, suggested_value) -> CorrectionSuggestion:
    suggestion = CorrectionSuggestion(
        issue_id=issue_id, source="RULE_BASED", suggested_value=suggested_value, confidence="1.0",
        category="DETERMINISTIC", fix_type="RULE_PROPOSED", reasoning=None, is_selected=False,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def _approve_and_stage(db, admin_user, review_run, resolved_issue_ids):
    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    approval_request = ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids, comment=None, actor=admin_user
    )
    db.expire_all()
    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    db.expire_all()
    return staging_run


# ---------------------------------------------------------------------------
# Core application behavior (Accept/Edit/Reject/Skip, composition, conflicts)
# ---------------------------------------------------------------------------


def test_accepted_correction_applied_to_row_snapshot(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_acc_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT, phone TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email', '555-1000')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_accept")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)

        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        assert staging_run.status == "READY"
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()
        assert record.row_snapshot["email"] == "fixed@example.com"
        assert record.row_snapshot["phone"] == "555-1000"  # unrelated field preserved
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_edited_correction_applied_using_edited_final_value(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_edit_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_edit")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "ai-suggested@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).edit(suggestion.id, "human-edited@example.com", admin_user)

        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()
        assert record.row_snapshot["email"] == "human-edited@example.com"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_rejected_correction_not_applied(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_rej_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email-one'), (2, 'bad-email-two')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_reject")
        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalars().all()
        assert len(issues) == 2
        accepted_issue, rejected_issue = issues[0], issues[1]

        accept_suggestion = _make_suggestion(db, accepted_issue.id, "fixed@example.com")
        reject_suggestion = _make_suggestion(db, rejected_issue.id, "irrelevant@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(accept_suggestion.id, admin_user)
        CorrectionDecisionService(db).reject(reject_suggestion.id, "not applicable", admin_user)

        staging_run = _approve_and_stage(db, admin_user, review_run, [accepted_issue.id])
        records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
        assert len(records) == 1
        assert records[0].record_ref == accepted_issue.record_ref
        assert records[0].row_snapshot["email"] == "fixed@example.com"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_skipped_correction_not_applied(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_skip_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email-one'), (2, 'bad-email-two')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_skip")
        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalars().all()
        accepted_issue, skipped_issue = issues[0], issues[1]

        accept_suggestion = _make_suggestion(db, accepted_issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(accept_suggestion.id, admin_user)
        CorrectionDecisionService(db).skip(skipped_issue.id, admin_user)

        staging_run = _approve_and_stage(db, admin_user, review_run, [accepted_issue.id])
        records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
        assert len(records) == 1
        assert records[0].record_ref == accepted_issue.record_ref
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_multiple_corrections_on_one_row_compose_safely(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_multi_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT, phone TEXT, amount NUMERIC)",
            "INSERT INTO {table} VALUES (1, 'bad-email', NULL, 9999)",
        )
        email_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        phone_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "phone")).scalar_one()
        amount_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "amount")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=email_col.id)
        _assign_rule(db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=phone_col.id)
        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000}, column_id=amount_col.id)

        review_run = _validate_and_review(db, admin_user, dataset, "p48_multi")
        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
        assert len(issues) == 3

        values_by_column = {"email": "fixed@example.com", "phone": "555-9999", "amount": "500"}
        suggestions = []
        for issue in issues:
            col_name = next(c.name for c in (email_col, phone_col, amount_col) if c.id == issue.column_id)
            suggestions.append((issue, _make_suggestion(db, issue.id, values_by_column[col_name])))

        _transition_to_review(db, admin_user, review_run)
        decision_service = CorrectionDecisionService(db)
        resolved_issue_ids = []
        for issue, suggestion in suggestions:
            decision_service.accept(suggestion.id, admin_user)
            resolved_issue_ids.append(issue.id)

        staging_run = _approve_and_stage(db, admin_user, review_run, resolved_issue_ids)
        assert staging_run.status == "READY"
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()
        assert record.row_snapshot["email"] == "fixed@example.com"
        assert record.row_snapshot["phone"] == "555-9999"
        assert float(record.row_snapshot["amount"]) == 500.0
        assert len(record.corrected_fields) == 3
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_conflicting_same_column_corrections_are_rejected_not_silently_resolved(db, redis_client, admin_user, pg_connection):
    """Two different enabled rules (two separate RANGE assignments, both
    violated by the same value) target the SAME column of the SAME row.
    Each produces its own Issue; if a human accepts DIFFERENT final
    values for the two, staging must refuse to silently pick one via
    last-one-wins overlay order — the whole run must fail with a clear
    integrity error (see build_corrected_fields, and its direct unit
    coverage in tests/unit/test_staging_record_builder.py)."""
    table_name = f"dq_p48_conflict_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, amount NUMERIC)",
            "INSERT INTO {table} VALUES (1, 5000)",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "amount")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000}, column_id=col.id)
        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 100}, column_id=col.id)

        review_run = _validate_and_review(db, admin_user, dataset, "p48_conflict")
        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalars().all()
        assert len(issues) == 2

        suggestion_one = _make_suggestion(db, issues[0].id, "50")
        suggestion_two = _make_suggestion(db, issues[1].id, "2000")  # conflicting final_value
        _transition_to_review(db, admin_user, review_run)
        decision_service = CorrectionDecisionService(db)
        decision_service.accept(suggestion_one.id, admin_user)
        decision_service.accept(suggestion_two.id, admin_user)

        staging_run = _approve_and_stage(db, admin_user, review_run, [issues[0].id, issues[1].id])
        assert staging_run.status == "FAILED"
        assert "Conflicting corrections" in (staging_run.error_message or "")
        records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
        assert records == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_source_row_never_mutated_by_staging_or_revalidation(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_immut_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_immut")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        StagedRevalidationService(db).revalidate_staging_record(record.id)

        source_value = db.execute(text(f"SELECT email FROM {table_name} WHERE id = 1")).scalar_one()
        assert source_value == "bad-email"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# Staged revalidation: PASS/FAIL for each row-local rule type
# ---------------------------------------------------------------------------


def test_pattern_fail_corrected_staged_value_revalidates_pass(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_pat_pass_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_pat_pass")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        pattern_reports = [r for r in reports if r.rule_type == "PATTERN"]
        assert len(pattern_reports) == 1
        assert pattern_reports[0].status == STATUS_REVALIDATED_PASS
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_pattern_accepted_but_still_invalid_revalidates_fail(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_pat_fail_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_pat_fail")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        # Human EDIT that is STILL invalid — must not be silently rewritten,
        # must not invoke AI, must not be marked fixed.
        suggestion = _make_suggestion(db, issue.id, "ai-suggestion@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).edit(suggestion.id, "still-invalid", admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()
        assert record.row_snapshot["email"] == "still-invalid"

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        pattern_reports = [r for r in reports if r.rule_type == "PATTERN"]
        assert pattern_reports[0].status == STATUS_REVALIDATED_FAIL
        assert pattern_reports[0].checked_value == "still-invalid"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_completeness_null_corrected_staged_value_revalidates_pass(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_comp_pass_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, phone TEXT)",
            "INSERT INTO {table} VALUES (1, NULL)",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "phone")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_comp_pass")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "555-0000")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        completeness_reports = [r for r in reports if r.rule_type == "COMPLETENESS"]
        assert completeness_reports[0].status == STATUS_REVALIDATED_PASS
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_range_invalid_corrected_staged_value_revalidates_pass(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_range_pass_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, amount NUMERIC)",
            "INSERT INTO {table} VALUES (1, -50)",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "amount")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_range_pass")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "100")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        range_reports = [r for r in reports if r.rule_type == "RANGE"]
        assert range_reports[0].status == STATUS_REVALIDATED_PASS
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_range_accepted_but_still_invalid_revalidates_fail(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_range_fail_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, amount NUMERIC)",
            "INSERT INTO {table} VALUES (1, -50)",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "amount")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="RANGE", definition={"min": 0, "max": 1000}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_range_fail")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "999")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).edit(suggestion.id, "-999", admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        range_reports = [r for r in reports if r.rule_type == "RANGE"]
        assert range_reports[0].status == STATUS_REVALIDATED_FAIL
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# Rule reuse, disabled rules, cross-row rules
# ---------------------------------------------------------------------------


def test_disabled_rule_not_evaluated_during_staged_revalidation(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_disabled_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT, phone TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email', NULL)",
        )
        email_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        phone_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "phone")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=email_col.id)
        _assign_rule(db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=phone_col.id, is_enabled=False)

        review_run = _validate_and_review(db, admin_user, dataset, "p48_disabled")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == email_col.id)).scalar_one()
        suggestion = _make_suggestion(db, issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        rule_types = {r.rule_type for r in reports}
        assert "PATTERN" in rule_types
        assert "COMPLETENESS" not in rule_types  # disabled rule never evaluated
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_uniqueness_rule_never_falsely_reported_pass_during_staged_revalidation(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_uniq_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT, code TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email', 'ABC')",
        )
        email_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        code_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "code")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=email_col.id)
        _assign_rule(db, admin_user, dataset, rule_type="UNIQUENESS", definition={"max_duplicate_percentage": 0}, column_id=code_col.id)

        review_run = _validate_and_review(db, admin_user, dataset, "p48_uniq")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == email_col.id)).scalar_one()
        suggestion = _make_suggestion(db, issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        reports = StagedRevalidationService(db).revalidate_staging_record(record.id)
        uniqueness_reports = [r for r in reports if r.rule_type == "UNIQUENESS"]
        assert len(uniqueness_reports) == 1
        assert uniqueness_reports[0].status == STATUS_REQUIRES_DATASET_REVALIDATION
        assert uniqueness_reports[0].status != STATUS_REVALIDATED_PASS
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_repeated_revalidation_is_idempotent(db, redis_client, admin_user, pg_connection):
    table_name = f"dq_p48_idem_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _setup(
            db, redis_client, admin_user, pg_connection, table_name,
            "CREATE TABLE {table} (id INT PRIMARY KEY, email TEXT)",
            "INSERT INTO {table} VALUES (1, 'bad-email')",
        )
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        _assign_rule(db, admin_user, dataset, rule_type="PATTERN", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}, column_id=col.id)
        review_run = _validate_and_review(db, admin_user, dataset, "p48_idem")
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id, Issue.column_id == col.id)).scalar_one()

        suggestion = _make_suggestion(db, issue.id, "fixed@example.com")
        _transition_to_review(db, admin_user, review_run)
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        staging_run = _approve_and_stage(db, admin_user, review_run, [issue.id])
        record = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalar_one()

        service = StagedRevalidationService(db)
        first = service.revalidate_staging_record(record.id)
        second = service.revalidate_staging_record(record.id)
        assert [(r.rule_type, r.status, r.checked_value) for r in first] == [
            (r.rule_type, r.status, r.checked_value) for r in second
        ]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# ---------------------------------------------------------------------------
# No AI, no source write, by construction
# ---------------------------------------------------------------------------


def test_no_ai_or_provider_write_imports_in_revalidation_modules():
    import app.modules.validation.staged_revalidation as pure_module
    import app.modules.staging.revalidation_service as service_module

    for module in (pure_module, service_module):
        source = inspect.getsource(module)
        assert "app.modules.ai" not in source
        assert "get_provider" not in source
        assert "UPDATE " not in source.upper()

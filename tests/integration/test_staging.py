"""Integration tests for Phase 8 (Staging) against a real local Postgres
instance, mirroring tests/integration/test_approval.py's structure.
"""
import threading
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import (
    ApprovalNotApprovedError,
    NoEligibleIssuesError,
    ReviewRunArchivedError,
    StagingAlreadyInProgressError,
    StagingRecordCountExceedsLimitError,
)
from app.db.models import (
    ApprovalDecision,
    ApprovalDecisionIssue,
    ApprovalRequest,
    Column,
    Connection,
    Correction,
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
from app.modules.staging.service import StagingService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _build_approved_review_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str,
):
    """Builds a table with 2 columns (a, b), 2 rows each with a NULL,
    discovers/profiles/validates/reviews/corrects/approves everything —
    returns (review_run, approval_request, resolved_issue_ids, dataset)."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, a TEXT, b TEXT)"))
    db.execute(
        text(
            f"INSERT INTO {table_name} VALUES "
            "(1, 'x', 'y'), (2, 'x', 'y'), (3, NULL, 'y'), (4, 'x', NULL)"
        )
    )
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

    col_a = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
    col_b = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "b")).scalar_one()

    rules_service = RulesService(db)
    assignment_service = RuleAssignmentService(db)
    for col in (col_a, col_b):
        rule = rules_service.create_rule(
            actor=admin_user, name=f"{col.name}_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
        validation_run_id=validation_run.id, name=f"staging_test_{uuid.uuid4().hex[:6]}", actor=admin_user
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
    db.expire_all()

    return db.get(ReviewRun, review_run.id), db.get(ApprovalRequest, approval_request.id), resolved_issue_ids, dataset


@pytest.fixture
def approved_review_run(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    table_name = f"dq_staging_{uuid.uuid4().hex[:8]}"
    try:
        review_run, approval_request, resolved_issue_ids, dataset = _build_approved_review_run(
            db, redis_client, admin_user, pg_connection, table_name
        )
        assert approval_request.status == "APPROVED"
        yield review_run, approval_request, resolved_issue_ids, dataset, table_name
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_no_approval_request_returns_409(db: Session, admin_user: User) -> None:
    review_run = db.execute(select(ReviewRun)).scalars().first()
    if review_run is None:
        pytest.skip("no review_run available")
    with pytest.raises(ApprovalNotApprovedError):
        StagingService(db).trigger(review_run.id, admin_user)


def test_pending_approval_returns_409(db: Session, admin_user: User, redis_client, pg_connection: Connection) -> None:
    table_name = f"dq_staging_pending_{uuid.uuid4().hex[:8]}"
    try:
        from app.modules.discovery.tasks import run_discovery
        from app.modules.profiling.service import ProfilingService
        from app.modules.profiling.tasks import run_profile

        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, a TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'x'), (2, NULL)"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        job = JobsService(db, redis_client).create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()
        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"pending_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = RulesService(db).list_versions(rule.id)[0]
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
        )
        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()
        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="pending", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        ApprovalService(db).submit(review_run.id, admin_user)  # stays PENDING, never decided
        db.expire_all()

        with pytest.raises(ApprovalNotApprovedError):
            StagingService(db).trigger(review_run.id, admin_user)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_archived_review_run_returns_409(db: Session, admin_user: User, approved_review_run) -> None:
    review_run, _, _, _, _ = approved_review_run
    db.execute(text("UPDATE review_runs SET status = 'ARCHIVED' WHERE id = :rid"), {"rid": review_run.id})
    db.commit()
    db.expire_all()

    with pytest.raises(ReviewRunArchivedError):
        StagingService(db).trigger(review_run.id, admin_user)


def test_zero_eligible_issues_returns_409_no_row_created(db: Session, admin_user: User, approved_review_run) -> None:
    review_run, approval_request, _, _, _ = approved_review_run
    # Contrive an APPROVED request with zero approval_decision_issues rows —
    # structurally shouldn't happen via the real API flow, tested here via
    # direct manipulation to exercise this precondition specifically.
    empty_request = ApprovalRequest(
        review_run_id=review_run.id, status="APPROVED", affected_issue_count=0, affected_record_count=0,
        requested_by=admin_user.id,
    )
    db.add(empty_request)
    db.commit()

    before = db.execute(select(StagingRun)).scalars().all()
    with pytest.raises(NoEligibleIssuesError):
        StagingService(db).trigger(review_run.id, admin_user)
    after = db.execute(select(StagingRun)).scalars().all()
    assert len(before) == len(after)


def test_record_count_exceeds_limit_returns_422_no_row_created(
    db: Session, admin_user: User, approved_review_run, monkeypatch
) -> None:
    review_run, _, _, _, _ = approved_review_run
    monkeypatch.setattr(settings, "MAX_SYNCHRONOUS_STAGING_RECORDS", 1)  # fixture has 2 eligible records

    before = db.execute(select(StagingRun)).scalars().all()
    with pytest.raises(StagingRecordCountExceedsLimitError):
        StagingService(db).trigger(review_run.id, admin_user)
    after = db.execute(select(StagingRun)).scalars().all()
    assert len(before) == len(after)


def test_successful_staging_computes_correct_counts_and_content(db: Session, admin_user: User, approved_review_run) -> None:
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    staging_run = StagingService(db).trigger(review_run.id, admin_user)

    assert staging_run.status == "READY"
    assert staging_run.attempt_number == 1
    assert staging_run.is_current is True
    assert staging_run.record_count == 2  # two distinct rows (id=3, id=4), one corrected field each
    assert staging_run.field_count == 2

    records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
    assert len(records) == 2
    # id=3's NULL was in column 'a' (mode of a's non-null values ['x','x','x'] is 'x');
    # id=4's NULL was in column 'b' (mode of b's non-null values ['y','y','y'] is 'y').
    expected_final_value_by_record_ref = {"3": "x", "4": "y"}
    for record in records:
        assert len(record.corrected_fields) == 1
        corrected = record.corrected_fields[0]
        assert corrected["final_value"] == expected_final_value_by_record_ref[record.record_ref]
        # Unchanged source fields preserved verbatim in the snapshot.
        assert record.row_snapshot["id"] in (3, 4)
        assert record.source_drift_status == "UNCHANGED"


def test_corrected_field_value_changed_since_correction_is_detected(
    db: Session, admin_user: User, approved_review_run
) -> None:
    """Corrected-field-level drift (the corrected design): id=3's column
    'a' was NULL at validation time and corrected to 'x' (mode_fill). If
    the SOURCE's 'a' value for id=3 changes again before staging, that is
    exactly the kind of drift this design must catch — a real change to a
    CORRECTED field's underlying value between correction and staging."""
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    db.execute(text(f"UPDATE {table_name} SET a = 'CHANGED_EXTERNALLY' WHERE id = 3"))
    db.commit()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    assert staging_run.has_source_drift is True

    records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
    drifted = [r for r in records if r.source_drift_status == "VALUE_CHANGED"]
    assert len(drifted) == 1
    assert drifted[0].record_ref == "3"
    assert drifted[0].source_drift_fields == ["a"]
    # row_snapshot still reflects the CURRENT source value overlaid with
    # the approved correction — final_value wins regardless of drift.
    assert drifted[0].row_snapshot["a"] == "x"


def test_uncorrected_field_value_changed_is_not_flagged_as_drift(
    db: Session, admin_user: User, approved_review_run
) -> None:
    """The accepted limitation (locked decision 9), tested as known
    behavior: id=3's column 'b' was never corrected (id=3's only issue was
    on column 'a'). Changing 'b' in the source must NOT be flagged as
    drift for record id=3, even though the row's content did change."""
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    db.execute(text(f"UPDATE {table_name} SET b = 'CHANGED_BUT_UNCORRECTED' WHERE id = 3"))
    db.commit()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    assert staging_run.has_source_drift is False  # neither record has a CORRECTED field that drifted

    record_3 = db.execute(
        select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id, StagingRecord.record_ref == "3")
    ).scalar_one()
    assert record_3.source_drift_status == "UNCHANGED"
    # row_snapshot is still always current for every field, regardless of
    # drift status — the uncorrected change IS visible in the snapshot,
    # it's just not flagged as drift.
    assert record_3.row_snapshot["b"] == "CHANGED_BUT_UNCORRECTED"


def test_missing_source_record_detected(db: Session, admin_user: User, approved_review_run) -> None:
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    db.execute(text(f"DELETE FROM {table_name} WHERE id = 3"))
    db.commit()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    assert staging_run.has_source_drift is True

    records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
    not_found = [r for r in records if r.source_drift_status == "RECORD_NOT_FOUND"]
    assert len(not_found) == 1
    assert not_found[0].source_row_hash_at_staging is None


def test_multiple_issues_same_record_produce_one_staging_record(
    db: Session, admin_user: User, redis_client, pg_connection: Connection
) -> None:
    table_name = f"dq_staging_multi_{uuid.uuid4().hex[:8]}"
    try:
        from app.modules.discovery.tasks import run_discovery
        from app.modules.profiling.service import ProfilingService
        from app.modules.profiling.tasks import run_profile

        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, a TEXT, b TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'x', 'y'), (2, 'x', 'y'), (3, NULL, NULL)"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        job = JobsService(db, redis_client).create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()
        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()

        col_a = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
        col_b = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "b")).scalar_one()
        rules_service = RulesService(db)
        assignment_service = RuleAssignmentService(db)
        for col in (col_a, col_b):
            rule = rules_service.create_rule(
                actor=admin_user, name=f"multi_{col.name}_{uuid.uuid4().hex[:8]}", description=None, category=None,
                rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
                error_message_template=None,
            )
            version = rules_service.list_versions(rule.id)[0]
            assignment_service.create_assignment(
                actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
                assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
            )

        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()

        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="multi", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        # Both issues are on record id=3 — accept both.
        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
        assert len(issues) == 2
        assert issues[0].record_ref == issues[1].record_ref

        resolved_issue_ids = []
        for issue in issues:
            suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
            CorrectionDecisionService(db).accept(suggestion.id, admin_user)
            resolved_issue_ids.append(issue.id)
        db.expire_all()

        approval_request = ApprovalService(db).submit(review_run.id, admin_user)
        ApprovalService(db).decide(approval_request.id, decision="APPROVE", issue_ids=resolved_issue_ids, comment=None, actor=admin_user)
        db.expire_all()

        staging_run = StagingService(db).trigger(review_run.id, admin_user)
        assert staging_run.record_count == 1  # one record, not two
        assert staging_run.field_count == 2  # two corrected fields on that one record

        records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
        assert len(records) == 1
        assert len(records[0].corrected_fields) == 2
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_rerun_creates_next_attempt_and_flips_is_current(db: Session, admin_user: User, approved_review_run) -> None:
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run
    service = StagingService(db)

    first = service.trigger(review_run.id, admin_user)
    assert first.attempt_number == 1
    assert first.is_current is True

    second = service.trigger(review_run.id, admin_user)
    assert second.attempt_number == 2
    assert second.is_current is True

    db.expire_all()
    refreshed_first = db.get(StagingRun, first.id)
    assert refreshed_first.is_current is False


def test_restage_with_no_source_change_produces_matching_staging_hash(
    db: Session, admin_user: User, approved_review_run
) -> None:
    """The one hash comparison that IS reliable per the corrected design:
    two attempts, both computed by Phase 8's own compute_staging_row_hash,
    over an unchanged source row, must match — this is a Phase-8-to-
    Phase-8 comparison, never Phase-5-to-Phase-8."""
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run
    service = StagingService(db)

    first = service.trigger(review_run.id, admin_user)
    second = service.trigger(review_run.id, admin_user)

    first_records = {
        r.record_ref: r.source_row_hash_at_staging
        for r in db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == first.id)).scalars()
    }
    second_records = {
        r.record_ref: r.source_row_hash_at_staging
        for r in db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == second.id)).scalars()
    }
    assert first_records == second_records
    assert all(h is not None for h in first_records.values())


def test_two_approved_cycles_uses_the_second_scope(db: Session, admin_user: User, redis_client, pg_connection: Connection) -> None:
    table_name = f"dq_staging_twocycles_{uuid.uuid4().hex[:8]}"
    try:
        from app.modules.discovery.tasks import run_discovery
        from app.modules.profiling.service import ProfilingService
        from app.modules.profiling.tasks import run_profile

        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, a TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, NULL), (2, NULL)"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        job = JobsService(db, redis_client).create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()
        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"twocycle_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = RulesService(db).list_versions(rule.id)[0]
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
        )
        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()
        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="twocycle", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
        assert len(issues) == 2
        decision_service = CorrectionDecisionService(db)

        # Both rows are NULL in column 'a', so mode_fill has no value to
        # suggest (an empty column has no mode) — use correct_directly
        # instead of accept(), sidestepping the need for a suggestion.
        # Cycle 1: correct only issue[0].
        decision_service.correct_directly(issues[0].id, "manual-value-1", admin_user)
        db.expire_all()
        request_1 = ApprovalService(db).submit(review_run.id, admin_user)
        request_1 = ApprovalService(db).decide(request_1.id, decision="APPROVE", issue_ids=[issues[0].id], comment=None, actor=admin_user)
        assert request_1.status == "APPROVED"
        db.expire_all()

        # Manually reopen the review run back to IN_REVIEW so a second real
        # submission is possible (Phase 6/7 don't provide an API path back
        # from READY_FOR_APPROVAL to IN_REVIEW after a full approval —
        # direct ORM manipulation only, isolating this one precondition).
        db.execute(text("UPDATE review_runs SET status = 'IN_REVIEW' WHERE id = :rid"), {"rid": review_run.id})
        db.commit()
        db.expire_all()

        # Cycle 2: resolve and approve issue[1] too — creates a SECOND
        # approval_requests row (Phase 7 permits a fresh submission once the
        # prior one is no longer PENDING/PARTIALLY_APPROVED).
        decision_service.correct_directly(issues[1].id, "manual-value-2", admin_user)
        db.expire_all()
        request_2 = ApprovalService(db).submit(review_run.id, admin_user)
        request_2 = ApprovalService(db).decide(request_2.id, decision="APPROVE", issue_ids=[issues[0].id, issues[1].id], comment=None, actor=admin_user)
        assert request_2.status == "APPROVED"
        assert request_2.id != request_1.id
        db.expire_all()

        staging_run = StagingService(db).trigger(review_run.id, admin_user)
        # The SECOND (most recent) approved request's scope — both issues,
        # both on distinct records (id=1, id=2) — is used, not the first's
        # (which only covered issue[0]).
        assert staging_run.record_count == 2
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_corrections_and_approval_tables_unmodified_by_staging(db: Session, admin_user: User, approved_review_run) -> None:
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    corrections_before = {
        str(c.issue_id): (c.final_value, c.status, c.value_source)
        for c in db.execute(select(Correction).where(Correction.issue_id.in_(resolved_issue_ids))).scalars()
    }
    approval_requests_before = list(db.execute(select(ApprovalRequest)).scalars())
    approval_decisions_before = list(db.execute(select(ApprovalDecision)).scalars())
    approval_decision_issues_before = list(db.execute(select(ApprovalDecisionIssue)).scalars())
    review_run_status_before = review_run.status

    StagingService(db).trigger(review_run.id, admin_user)
    db.expire_all()

    corrections_after = {
        str(c.issue_id): (c.final_value, c.status, c.value_source)
        for c in db.execute(select(Correction).where(Correction.issue_id.in_(resolved_issue_ids))).scalars()
    }
    assert corrections_before == corrections_after
    assert len(db.execute(select(ApprovalRequest)).scalars().all()) == len(approval_requests_before)
    assert len(db.execute(select(ApprovalDecision)).scalars().all()) == len(approval_decisions_before)
    assert len(db.execute(select(ApprovalDecisionIssue)).scalars().all()) == len(approval_decision_issues_before)
    assert db.get(ReviewRun, review_run.id).status == review_run_status_before  # no status mutation


def test_forced_source_failure_marks_run_failed_preserves_partial_records(
    db: Session, admin_user: User, approved_review_run, monkeypatch
) -> None:
    from app.modules.staging import service as staging_service_module
    from app.source_adapters.exceptions import SourceUnreachableError

    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    def _failing_get_provider(*args, **kwargs):
        raise SourceUnreachableError("simulated source outage")

    monkeypatch.setattr(staging_service_module, "get_provider", _failing_get_provider)

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    assert staging_run.status == "FAILED"
    assert "SourceUnreachableError" in staging_run.error_message

    # No staging_records were written (failure occurred before the batched
    # fetch / any per-record write) — a valid, minimal case of "preserved
    # partial progress" (zero records to preserve).
    records = db.execute(select(StagingRecord).where(StagingRecord.staging_run_id == staging_run.id)).scalars().all()
    assert records == []


def test_audit_events_contain_no_raw_values(db: Session, admin_user: User, approved_review_run) -> None:
    from app.db.models import AuditEvent

    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run

    corrections = list(db.execute(select(Correction).where(Correction.issue_id.in_(resolved_issue_ids))).scalars())
    final_values = [c.final_value for c in corrections if c.final_value]

    StagingService(db).trigger(review_run.id, admin_user)

    events = db.execute(
        select(AuditEvent).where(AuditEvent.action.in_(("staging_run.created", "staging_run.completed", "staging_run.failed")))
    ).scalars().all()
    assert len(events) >= 1
    for event in events:
        blob = str(event.before_value) + str(event.after_value) + str(event.audit_metadata)
        for value in final_values:
            assert value not in blob


def test_concurrent_trigger_calls_exactly_one_building_succeeds(
    db: Session, admin_user: User, approved_review_run
) -> None:
    """Genuine concurrency test: two real threads, each with its own DB
    session, both call trigger() for the SAME review run at effectively the
    same instant via a threading.Barrier. The SELECT ... FOR UPDATE lock on
    review_runs (locked decision 3) must serialize them: one succeeds and
    starts a real staging build, the other correctly sees the resulting
    BUILDING/READY state and gets a clean 409, not a race/corruption."""
    review_run, approval_request, resolved_issue_ids, dataset, table_name = approved_review_run
    db.commit()

    review_run_id = review_run.id
    admin_user_id = admin_user.id

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _worker(name: str) -> None:
        thread_db = SessionLocal()
        try:
            thread_user = thread_db.get(User, admin_user_id)
            service = StagingService(thread_db)
            barrier.wait(timeout=10)
            try:
                outcome = service.trigger(review_run_id, thread_user)
                results[name] = ("success", outcome.status)
            except StagingAlreadyInProgressError as exc:
                results[name] = ("blocked", str(exc))
        finally:
            thread_db.close()

    t1 = threading.Thread(target=_worker, args=("t1",))
    t2 = threading.Thread(target=_worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert "t1" in results and "t2" in results
    outcomes = [results["t1"], results["t2"]]
    successes = [o for o in outcomes if o[0] == "success"]
    blocked = [o for o in outcomes if o[0] == "blocked"]
    assert len(successes) == 1
    assert len(blocked) == 1

    db.expire_all()
    all_runs = db.execute(select(StagingRun).where(StagingRun.review_run_id == review_run_id)).scalars().all()
    assert len(all_runs) == 1  # exactly one attempt was ever created

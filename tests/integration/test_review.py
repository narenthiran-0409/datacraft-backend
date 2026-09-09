"""Integration tests for Phase 6 (Review + Corrections) against a real
local Postgres instance, mirroring tests/integration/test_validation.py's
structure and conventions.
"""
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.exceptions import (
    EmptyFinalValueError,
    InvalidReviewStatusTransitionError,
    SourceValidationRunIncompleteError,
    SuggestionAlreadyDecidedError,
)
from app.db.models import (
    Column,
    Connection,
    Correction,
    CorrectionSuggestion,
    Dataset,
    Issue,
    ReviewRun,
    Schema,
    User,
    ValidationRun,
)
from app.modules.jobs.service import JobsService
from app.modules.review.decision_service import CorrectionDecisionService
from app.modules.review.service import ReviewService
from app.modules.review.suggestion_service import SuggestionService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation
import pytest


def _build_reviewable_validation_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str
) -> ValidationRun:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    db.execute(
        text(
            f"CREATE TABLE {table_name} "
            "(id INT PRIMARY KEY, category TEXT, amount NUMERIC, code TEXT, dupe_key TEXT)"
        )
    )
    db.execute(
        text(
            f"INSERT INTO {table_name} VALUES "
            "(1, 'ALPHA', 10, 'AB-123', 'dupval'), "
            "(2, NULL, 20, 'AB-124', 'dupval'), "
            "(3, 'BETA', NULL, '  AB-125  ', 'y'), "
            "(4, 'ALPHA', 500, 'bad-code', 'z')"
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

    # A completed profile run is required for mode_fill/median_fill to have
    # column_profiles data to suggest from.
    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    category_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "category")).scalar_one()
    amount_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "amount")).scalar_one()
    code_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "code")).scalar_one()
    dupe_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "dupe_key")).scalar_one()

    rules_service = RulesService(db)
    assignment_service = RuleAssignmentService(db)

    def _assign(rule_type, definition, column_id):
        rule = rules_service.create_rule(
            actor=admin_user, name=f"{rule_type}_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type=rule_type, origin="CUSTOM", definition=definition, severity="HIGH", error_message_template=None,
        )
        version = rules_service.list_versions(rule.id)[0]
        assignment_service.create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=column_id, column_ids=None, template_id=None,
        )

    _assign("COMPLETENESS", {"max_null_percentage": 0}, category_col.id)
    _assign("COMPLETENESS", {"max_null_percentage": 0}, amount_col.id)
    _assign("RANGE", {"min": 0, "max": 100}, amount_col.id)
    _assign("PATTERN", {"regex": r"^[A-Z]{2}-\d{3}$"}, code_col.id)
    _assign("UNIQUENESS", {"max_duplicate_percentage": 0}, dupe_col.id)

    validation_run, validation_job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()

    return db.get(ValidationRun, validation_run.id)


@pytest.fixture
def reviewable_validation_run(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    table_name = f"dq_review_{uuid.uuid4().hex[:8]}"
    try:
        yield _build_reviewable_validation_run(db, redis_client, admin_user, pg_connection, table_name)
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_create_review_run_rejects_incomplete_validation_run(db: Session, admin_user: User, reviewable_validation_run) -> None:
    incomplete = ValidationRun(dataset_id=reviewable_validation_run.dataset_id, status="RUNNING")
    db.add(incomplete)
    db.commit()

    with pytest.raises(SourceValidationRunIncompleteError):
        ReviewService(db).create_from_validation_run(validation_run_id=incomplete.id, name=None, actor=admin_user)


def test_create_review_run_creates_one_issue_per_validation_failure(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name="test review", actor=admin_user
    )
    issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
    assert len(issues) == 7  # 1 + 1 + 1 + 2 + 2, per the fixture's constructed data
    assert all(i.status == "PENDING" for i in issues)
    assert review_run.status == "DRAFT"


def test_generate_suggestions_fires_correct_generators_and_skips_uniqueness(
    db: Session, admin_user: User, reviewable_validation_run
) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    result = SuggestionService(db).generate_for_review_run(review_run.id, admin_user)

    assert result["generated_count"] == 4
    assert result["issues_with_no_suggestion_count"] == 3

    suggestions = db.execute(
        select(CorrectionSuggestion)
        .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
        .where(Issue.review_run_id == review_run.id)
    ).scalars().all()
    fix_types = sorted(s.fix_type for s in suggestions)
    assert fix_types == sorted(["mode_fill", "median_fill", "range_clamp", "trim_whitespace"])
    assert all(s.source == "RULE_BASED" for s in suggestions)

    # mode_fill suggested value for category is 'ALPHA' (appears twice out of 3 non-null)
    mode_suggestion = next(s for s in suggestions if s.fix_type == "mode_fill")
    assert mode_suggestion.suggested_value == "ALPHA"

    # median_fill for amount ([10,20,500] -> median 20)
    median_suggestion = next(s for s in suggestions if s.fix_type == "median_fill")
    assert float(median_suggestion.suggested_value) == 20.0

    # range_clamp clamps 500 down to the configured max of 100
    range_suggestion = next(s for s in suggestions if s.fix_type == "range_clamp")
    assert float(range_suggestion.suggested_value) == 100.0

    # trim_whitespace strips '  AB-125  ' -> 'AB-125'
    trim_suggestion = next(s for s in suggestions if s.fix_type == "trim_whitespace")
    assert trim_suggestion.suggested_value == "AB-125"

    # UNIQUENESS issues (dupe_key, both rows had value 'dupval') have zero suggestions
    uniqueness_issues = db.execute(
        select(Issue).where(Issue.review_run_id == review_run.id, Issue.original_value == "dupval")
    ).scalars().all()
    assert len(uniqueness_issues) == 2
    for issue in uniqueness_issues:
        count = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)
        ).scalars().all()
        assert len(count) == 0


def test_generate_suggestions_is_idempotent_for_already_suggested_issues(
    db: Session, admin_user: User, reviewable_validation_run
) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    first = SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    second = SuggestionService(db).generate_for_review_run(review_run.id, admin_user)

    assert first["generated_count"] == 4
    assert first["issues_with_no_suggestion_count"] == 3
    # The 4 issues that already have a suggestion are excluded from the second
    # pass (per spec: "zero existing suggestions" is the inclusion filter).
    # The 3 issues that produced no suggestion are NOT excluded — they have no
    # suggestion rows to signal "already processed" — so they are correctly
    # re-attempted, again yielding zero new suggestions each time.
    assert second["generated_count"] == 0
    assert second["issues_with_no_suggestion_count"] == 3


def _get_issue_with_suggestion(db: Session, review_run_id, fix_type: str):
    suggestion = db.execute(
        select(CorrectionSuggestion)
        .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
        .where(Issue.review_run_id == review_run_id, CorrectionSuggestion.fix_type == fix_type)
    ).scalar_one()
    issue = db.get(Issue, suggestion.issue_id)
    return issue, suggestion


def test_accept_upserts_correction_and_resolves_issue_with_one_audit_event(
    db: Session, admin_user: User, reviewable_validation_run
) -> None:
    from app.db.models import AuditEvent

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    issue, suggestion = _get_issue_with_suggestion(db, review_run.id, "mode_fill")

    correction = CorrectionDecisionService(db).accept(suggestion.id, admin_user)

    db.expire_all()
    updated_issue = db.get(Issue, issue.id)
    assert updated_issue.status == "RESOLVED"
    assert correction.status == "ACCEPTED"
    assert correction.final_value == suggestion.suggested_value
    assert correction.value_source == "RULE_BASED"

    audit_rows = db.execute(
        select(AuditEvent).where(AuditEvent.entity_type == "ISSUE", AuditEvent.entity_id == issue.id, AuditEvent.action == "issue.accepted")
    ).scalars().all()
    assert len(audit_rows) == 1
    # CRITICAL: no raw value in audit metadata.
    metadata_blob = str(audit_rows[0].audit_metadata)
    assert suggestion.suggested_value not in metadata_blob


def test_reaccept_is_allowed_and_updates_existing_correction_row(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    issue, suggestion = _get_issue_with_suggestion(db, review_run.id, "range_clamp")

    service = CorrectionDecisionService(db)
    first = service.accept(suggestion.id, admin_user)
    second = service.accept(suggestion.id, admin_user)  # re-accept, allowed

    assert first.id == second.id  # same row, upserted in place
    rows = db.execute(select(Correction).where(Correction.issue_id == issue.id)).scalars().all()
    assert len(rows) == 1


def test_edit_sets_human_source_and_resolves_issue(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    issue, suggestion = _get_issue_with_suggestion(db, review_run.id, "trim_whitespace")

    correction = CorrectionDecisionService(db).edit(suggestion.id, "AB-999", admin_user)

    assert correction.final_value == "AB-999"
    assert correction.value_source == "HUMAN"
    assert correction.status == "EDITED"
    db.expire_all()
    assert db.get(Issue, issue.id).status == "RESOLVED"


def test_edit_rejects_blank_final_value(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    _, suggestion = _get_issue_with_suggestion(db, review_run.id, "trim_whitespace")

    with pytest.raises(EmptyFinalValueError):
        CorrectionDecisionService(db).edit(suggestion.id, "   ", admin_user)


def test_reject_is_terminal_and_blocks_further_action(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    issue, suggestion = _get_issue_with_suggestion(db, review_run.id, "median_fill")

    service = CorrectionDecisionService(db)
    correction = service.reject(suggestion.id, "not applicable", admin_user)
    assert correction.status == "REJECTED"
    db.expire_all()
    assert db.get(Issue, issue.id).status == "RESOLVED"

    with pytest.raises(SuggestionAlreadyDecidedError):
        service.accept(suggestion.id, admin_user)


def test_skip_is_terminal_and_blocks_further_action(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
    target_issue = issues[0]

    service = CorrectionDecisionService(db)
    correction = service.skip(target_issue.id, admin_user)
    assert correction.status == "SKIPPED"
    db.expire_all()
    assert db.get(Issue, target_issue.id).status == "SKIPPED"

    with pytest.raises(SuggestionAlreadyDecidedError):
        service.skip(target_issue.id, admin_user)


def test_correct_directly_for_issue_with_zero_suggestions(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)

    # Find a UNIQUENESS issue (zero suggestions) via original_value == 'dupval'
    no_suggestion_issue = db.execute(
        select(Issue).where(Issue.review_run_id == review_run.id, Issue.original_value == "dupval")
    ).scalars().first()
    assert no_suggestion_issue is not None

    correction = CorrectionDecisionService(db).correct_directly(no_suggestion_issue.id, "manual-fix", admin_user)
    assert correction.correction_suggestion_id is None
    assert correction.value_source == "HUMAN"
    assert correction.status == "EDITED"
    db.expire_all()
    assert db.get(Issue, no_suggestion_issue.id).status == "RESOLVED"


def test_bulk_action_skip_is_atomic_across_all_issues(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
    issue_ids = [i.id for i in issues[:3]]

    result = CorrectionDecisionService(db).bulk_action(
        review_run_id=review_run.id, issue_ids=issue_ids, action="skip", actor=admin_user
    )
    assert result["issue_count"] == 3
    db.expire_all()
    for issue_id in issue_ids:
        assert db.get(Issue, issue_id).status == "SKIPPED"


def test_bulk_action_fails_atomically_if_any_issue_already_terminal(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
    already_skipped = issues[0]
    CorrectionDecisionService(db).skip(already_skipped.id, admin_user)

    issue_ids = [issues[0].id, issues[1].id]
    with pytest.raises(SuggestionAlreadyDecidedError):
        CorrectionDecisionService(db).bulk_action(
            review_run_id=review_run.id, issue_ids=issue_ids, action="skip", actor=admin_user
        )

    # Nothing else in the batch should have been mutated (atomic failure).
    db.expire_all()
    assert db.get(Issue, issues[1].id).status == "PENDING"


def test_archive_and_restore_state_machine(db: Session, admin_user: User, reviewable_validation_run) -> None:
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    service = ReviewService(db)

    archived = service.archive(review_run.id, admin_user)
    assert archived.status == "ARCHIVED"
    assert archived.archived_at is not None

    with pytest.raises(InvalidReviewStatusTransitionError):
        service.archive(review_run.id, admin_user)  # already archived

    restored = service.restore(review_run.id, admin_user)
    assert restored.status == "IN_REVIEW"
    assert restored.archived_at is None

    with pytest.raises(InvalidReviewStatusTransitionError):
        service.restore(review_run.id, admin_user)  # not archived


def test_ai_suggestion_id_check_constraint_enforced_without_fk(db: Session, admin_user: User, reviewable_validation_run) -> None:
    """Confirms the authorized deviation: ai_suggestion_id has no FK (would
    fail if ai_suggestions existed and this insert referenced a bad id —
    here it just proves the CHECK constraint alone still rejects
    source='AI' with a null ai_suggestion_id, entirely independent of any
    FK)."""
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=reviewable_validation_run.id, name=None, actor=admin_user
    )
    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()

    from sqlalchemy.exc import IntegrityError

    bad = CorrectionSuggestion(
        issue_id=issue.id, source="AI", ai_suggestion_id=None, suggested_value="x",
        confidence=Decimal("0.5"), fix_type="fake", is_selected=False,
    )
    db.add(bad)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()

    # A NULL ai_suggestion_id IS allowed (no FK to violate) as long as source != 'AI'.
    ok = CorrectionSuggestion(
        issue_id=issue.id, source="RULE_BASED", ai_suggestion_id=None, suggested_value="x",
        confidence=Decimal("0.5"), fix_type="fake", is_selected=False,
    )
    db.add(ok)
    db.flush()  # should not raise
    db.rollback()

"""Integration tests for RuleDetectionService — the whole-dataset pattern +
AI-fallback candidate-rule detector — against a real local Postgres table.
The AI provider is always mocked (zero real external LLM calls, matching
tests/integration/test_ai_suggestions.py's convention); the pattern half
needs no mock at all, since it never calls out anywhere."""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import InvalidRuleReviewTransitionError, RulePromotionNotSupportedError
from app.db.models import (
    AIPromptVersion,
    AISuggestion,
    Connection,
    Dataset,
    Rule,
    RuleAssignment,
    RuleVersion,
    Schema,
    User,
    ValidationFailure,
    ValidationResult,
)
from app.modules.ai.providers import ProviderResponse
from app.modules.rules.detection_service import RuleDetectionService
from app.modules.rules.service import RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _create_prompt_version(db: Session, admin_user: User, prompt_key: str) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_provider(text_content: str):
    fake_response = ProviderResponse(
        content=text_content, input_tokens=10, output_tokens=10, latency_ms=5, raw_metadata={}
    )
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _make_profiled_dataset(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str,
    create_sql: str, insert_sql: str | None,
) -> Dataset:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    db.execute(text(create_sql))
    if insert_sql:
        db.execute(text(insert_sql))
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
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=True,
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()
    return db.get(Dataset, dataset.id)


def test_pattern_half_creates_pending_review_rules_without_any_llm_call(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_detect_pattern_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)",
            insert_sql=f"INSERT INTO {table_name} VALUES (1,'a@example.com'),(2,'b@example.com'),(3,'c@example.com')",
        )

        result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)

        assert result.ai_fallback_columns_considered == 0
        assert len(result.pattern_detected) == 2
        by_column = {d.column_name: d for d in result.pattern_detected}
        assert by_column["id"].rule.category == "id"
        assert by_column["email"].rule.category == "email"

        for detected in result.pattern_detected:
            assert detected.rule.origin == "PATTERN_DETECTED"
            assert detected.rule.status == "PENDING_REVIEW"

        # Semantic category is recorded on the column too (RULE_BASED, no
        # AI suggestion involved — this half never calls an LLM).
        db.expire_all()
        from app.db.models import Column

        email_col = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")
        ).scalar_one()
        assert email_col.semantic_category == "email"
        assert email_col.semantic_category_source == "RULE_BASED"
        assert email_col.semantic_category_ai_suggestion_id is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_fallback_makes_exactly_one_batched_call_for_all_low_confidence_columns(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_detect_ai_{uuid.uuid4().hex[:8]}"
    try:
        # Repeated values, not two all-distinct rows: with only a couple
        # of rows, ANY free-text column looks "100% unique" by chance,
        # which is itself a real id-like signal (correctly tested
        # separately in test_id_detected_by_uniqueness_alone_without_name_hint).
        # This test wants genuinely low-signal columns, so distinct_percentage
        # must actually be low.
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (notes TEXT, remarks TEXT)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "('x','a'),('x','a'),('y','b'),('y','b'),('z','c')"
            ),
        )
        _create_prompt_version(db, admin_user, "ai_rule_recommendation")

        llm_response = (
            '[{"column_name": "notes", "rule_type": "COMPLETENESS", '
            '"definition": {"max_null_percentage": 0}, "confidence": 0.7, "reasoning": "test"}]'
        )
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        mock_provider_cls = _mock_provider(llm_response)

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": mock_provider_cls}):
            result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)

        assert mock_provider_cls.return_value.send.call_count == 1  # one call for the whole batch, not per column

        assert result.ai_fallback_columns_considered == 2
        assert result.ai_fallback_columns_capped == 0
        assert len(result.pattern_detected) == 0
        assert len(result.ai_recommended) == 1
        assert result.ai_recommended[0].column_name == "notes"
        assert result.ai_recommended[0].rule.origin == "AI_RECOMMENDED"
        assert result.ai_recommended[0].rule.status == "PENDING_REVIEW"

        suggestions = db.execute(
            select(AISuggestion).where(AISuggestion.source_context_id == dataset.id)
        ).scalars().all()
        assert len(suggestions) == 1
        assert suggestions[0].suggestion_type == "RULE_RECOMMENDATION"
        assert suggestions[0].source_context_type == "DATASET"
        assert suggestions[0].status == "PROPOSED"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_disabled_degrades_gracefully_without_discarding_pattern_matched_results(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    table_name = f"dq_detect_degraded_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, notes TEXT)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES (1,'x'),(2,'x'),(3,'y'),(4,'y'),(5,'z')"
            ),
        )
        monkeypatch.setattr(settings, "AI_ENABLED", False)

        result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)

        assert len(result.pattern_detected) == 1  # the "id" column, unaffected by AI being off
        assert result.pattern_detected[0].column_name == "id"
        assert result.ai_fallback_columns_considered == 1  # "notes" needed AI, which wasn't available
        assert len(result.ai_recommended) == 0
        assert result.ai_skipped_reason is not None
        assert "AIDisabledError" in result.ai_skipped_reason
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_promote_without_detection_metadata_is_rejected_not_silently_left_unassigned(
    db: Session, admin_user: User
) -> None:
    """A PENDING_REVIEW rule with no _detected_for metadata (e.g. never
    went through the detector at all) can't have its assignment derived —
    promote_rule() must refuse rather than flip it to ACTIVE with nothing
    behind it, which is the exact original bug reintroduced via a
    different path."""
    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"promote_no_meta_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="PATTERN_DETECTED", definition={"max_null_percentage": 0},
        severity="MEDIUM", error_message_template=None,
    )
    assert rule.status == "PENDING_REVIEW"

    with pytest.raises(RulePromotionNotSupportedError):
        RulesService(db).promote_rule(actor=admin_user, rule_id=rule.id)

    unchanged = db.get(Rule, rule.id)
    assert unchanged.status == "PENDING_REVIEW"  # rejected cleanly, not left half-promoted


def test_promote_creates_a_real_single_column_assignment_and_rejects_double_promote(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_promote_single_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)",
            insert_sql=f"INSERT INTO {table_name} VALUES (1,'a@example.com')",
        )
        result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)
        detected = next(d for d in result.pattern_detected if d.column_name == "email")
        rule = detected.rule
        assert rule.rule_type == "PATTERN"

        assert (
            db.execute(select(RuleAssignment).where(RuleAssignment.rule_version_id.in_(
                select(RuleVersion.id).where(RuleVersion.rule_id == rule.id)
            ))).scalars().all()
            == []
        )

        promoted = RulesService(db).promote_rule(actor=admin_user, rule_id=rule.id)
        assert promoted.status == "ACTIVE"

        version_id = db.execute(
            select(RuleVersion.id).where(RuleVersion.rule_id == rule.id, RuleVersion.is_current.is_(True))
        ).scalar_one()
        assignment = db.execute(
            select(RuleAssignment).where(RuleAssignment.rule_version_id == version_id)
        ).scalar_one()
        assert assignment.dataset_id == dataset.id
        assert assignment.assignment_scope == "SINGLE_COLUMN"
        assert assignment.column_id == detected.column_id
        assert assignment.is_enabled is True

        with pytest.raises(InvalidRuleReviewTransitionError):
            RulesService(db).promote_rule(actor=admin_user, rule_id=rule.id)  # already ACTIVE
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_promote_maps_duplicate_rule_type_to_dataset_level_assignment(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """DUPLICATE's evaluator (evaluate_duplicate) takes no column at all —
    a whole-row check — so its assignment must be DATASET_LEVEL
    (column_id=NULL), even though the AI-fallback response shape (the
    only current path that could recommend DUPLICATE) always records one
    column_id in _detected_for alongside it."""
    table_name = f"dq_promote_dup_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)",
            insert_sql=f"INSERT INTO {table_name} VALUES (1,'a')",
        )
        from app.db.models import Column

        val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"dup_test_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="DUPLICATE", origin="AI_RECOMMENDED", definition={
                "_detected_for": {"dataset_id": str(dataset.id), "column_id": str(val_col.id), "confidence": 0.7},
            },
            severity="MEDIUM", error_message_template=None,
        )

        RulesService(db).promote_rule(actor=admin_user, rule_id=rule.id)

        version_id = db.execute(select(RuleVersion.id).where(RuleVersion.rule_id == rule.id)).scalar_one()
        assignment = db.execute(
            select(RuleAssignment).where(RuleAssignment.rule_version_id == version_id)
        ).scalar_one()
        assert assignment.assignment_scope == "DATASET_LEVEL"
        assert assignment.column_id is None  # the recorded column_id is intentionally not used
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_promote_cross_column_rule_is_refused_not_guessed(db: Session, admin_user: User) -> None:
    """Detection only ever records one column_id, but CROSS_COLUMN needs
    2+ specific columns via rule_assignment_columns — there's no honest
    way to derive that from what's stored, so promotion must refuse."""
    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"cross_test_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="CROSS_COLUMN", origin="AI_RECOMMENDED",
        definition={
            "check": "all_equal",
            "_detected_for": {"dataset_id": str(uuid.uuid4()), "column_id": str(uuid.uuid4()), "confidence": 0.6},
        },
        severity="MEDIUM", error_message_template=None,
    )

    with pytest.raises(RulePromotionNotSupportedError):
        RulesService(db).promote_rule(actor=admin_user, rule_id=rule.id)

    assert db.get(Rule, rule.id).status == "PENDING_REVIEW"


def test_dismiss_requires_pending_review_and_disables_without_deleting(db: Session, admin_user: User) -> None:
    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"dismiss_test_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="AI_RECOMMENDED", definition={"max_null_percentage": 0},
        severity="MEDIUM", error_message_template=None,
    )

    dismissed = RulesService(db).dismiss_rule(actor=admin_user, rule_id=rule.id)
    assert dismissed.status == "DISABLED"
    assert db.get(Rule, rule.id) is not None  # never a physical delete

    with pytest.raises(InvalidRuleReviewTransitionError):
        RulesService(db).dismiss_rule(actor=admin_user, rule_id=rule.id)  # already DISABLED


def test_dismiss_never_has_an_assignment_to_clean_up(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """Verified directly, not assumed: promote_rule() is the only code
    path that ever creates a rule_assignment for a detected rule, and it
    only runs on a PENDING_REVIEW -> ACTIVE transition. A rule dismissed
    while still PENDING_REVIEW can therefore never have one — confirmed
    here against a real detected rule rather than inferred from reading
    the code alone."""
    table_name = f"dq_dismiss_clean_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)",
            insert_sql=f"INSERT INTO {table_name} VALUES (1,'a@example.com')",
        )
        result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)
        rule = result.pattern_detected[0].rule

        RulesService(db).dismiss_rule(actor=admin_user, rule_id=rule.id)

        version_ids = db.execute(select(RuleVersion.id).where(RuleVersion.rule_id == rule.id)).scalars().all()
        assignments = db.execute(
            select(RuleAssignment).where(RuleAssignment.rule_version_id.in_(version_ids))
        ).scalars().all()
        assert assignments == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_create_rule_forces_pending_review_for_ai_and_pattern_origins_even_if_not_requested(
    db: Session, admin_user: User
) -> None:
    """The actual structural guarantee, tested directly against
    RulesService.create_rule() rather than only through the detector —
    proves the enforcement point itself, not just its two current
    callers."""
    for origin in ("AI_RECOMMENDED", "PATTERN_DETECTED"):
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"guard_{origin}_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin=origin, definition={"max_null_percentage": 0},
            severity="MEDIUM", error_message_template=None,
        )
        assert rule.status == "PENDING_REVIEW"

    for origin in ("BUILT_IN", "CUSTOM"):
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"guard_{origin}_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin=origin, definition={"max_null_percentage": 0},
            severity="MEDIUM", error_message_template=None,
        )
        assert rule.status == "ACTIVE"


def test_pending_review_rules_are_never_picked_up_by_a_real_validation_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """The hard requirement, verified directly rather than assumed: after
    a real detection run and a real validation run on the same dataset,
    the actual assignment-resolution query run_validation() itself uses
    (RuleAssignment.dataset_id == ..., is_enabled=True) returns nothing
    tied to any rule this detector created."""
    table_name = f"dq_detect_isolation_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)",
            insert_sql=f"INSERT INTO {table_name} VALUES (1,'a@example.com')",
        )

        result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)
        assert len(result.pattern_detected) > 0
        detected_rule_ids = [d.rule.id for d in result.pattern_detected]
        detected_version_ids = set(
            db.execute(select(RuleVersion.id).where(RuleVersion.rule_id.in_(detected_rule_ids))).scalars()
        )
        assert detected_version_ids  # sanity: versions really were created

        assignments_before = db.execute(
            select(RuleAssignment).where(RuleAssignment.rule_version_id.in_(detected_version_ids))
        ).scalars().all()
        assert assignments_before == []

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        completed_run = db.get(type(validation_run), validation_run.id)
        assert completed_run.status == "COMPLETED"

        assignments_after = db.execute(
            select(RuleAssignment).where(RuleAssignment.rule_version_id.in_(detected_version_ids))
        ).scalars().all()
        assert assignments_after == []  # still zero after a real validation run touched this dataset
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_promoted_rule_is_genuinely_evaluated_by_a_real_validation_run_pass_and_fail(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """The actual proof this fix exists for: a promoted rule must produce
    real pass/fail results against real data, not just leave an
    assignment row sitting there. Table has both a clean row (valid
    email) and a dirty row (not an email) so the PATTERN rule the
    detector proposes for the email column has something real to
    disagree with, not merely something to trivially pass."""
    table_name = f"dq_promote_e2e_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_profiled_dataset(
            db, redis_client, admin_user, pg_connection, table_name,
            create_sql=f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)",
            insert_sql=(
                f"INSERT INTO {table_name} VALUES "
                "(1,'a@example.com'),(2,'b@example.com'),(3,'not-an-email')"
            ),
        )

        result = RuleDetectionService(db).detect_for_dataset(dataset.id, admin_user)
        detected = next(d for d in result.pattern_detected if d.column_name == "email")
        assert detected.rule.rule_type == "PATTERN"

        promoted = RulesService(db).promote_rule(actor=admin_user, rule_id=detected.rule.id)
        assert promoted.status == "ACTIVE"

        version_id = db.execute(
            select(RuleVersion.id).where(RuleVersion.rule_id == promoted.id, RuleVersion.is_current.is_(True))
        ).scalar_one()
        assignment = db.execute(
            select(RuleAssignment).where(RuleAssignment.rule_version_id == version_id)
        ).scalar_one()
        assert assignment.is_enabled is True
        assert assignment.dataset_id == dataset.id

        validation_run, job = ValidationService(db).start_validation(
            actor=admin_user, dataset_id=dataset.id, template_id=None
        )
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        completed_run = db.get(type(validation_run), validation_run.id)
        assert completed_run.status == "COMPLETED"
        assert completed_run.total_rows == 3

        failures = db.execute(
            select(ValidationFailure).where(
                ValidationFailure.validation_run_id == completed_run.id,
                ValidationFailure.rule_assignment_id == assignment.id,
            )
        ).scalars().all()
        assert len(failures) == 1  # exactly the 'not-an-email' row — a real, specific fail
        assert failures[0].failed_value == "not-an-email"

        results = db.execute(
            select(ValidationResult).where(ValidationResult.validation_run_id == completed_run.id)
        ).scalars().all()
        passed = [r for r in results if r.status == "PASSED"]
        failed = [r for r in results if r.status != "PASSED"]
        assert len(passed) == 2  # the two real emails — a real, specific pass
        assert len(failed) == 1
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

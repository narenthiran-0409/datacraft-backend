"""Phase 4.10 acceptance fix: AISuggestionService.generate_corrections()
must transition a DRAFT review run to IN_REVIEW, exactly mirroring
SuggestionService.generate_for_review_run()'s own established Phase 7
behavior — otherwise a review run whose only review action was ever
"Generate AI Suggestions" stays DRAFT forever, and ApprovalService.submit()
(which requires IN_REVIEW) can never succeed even after every issue is
decided. Provider always mocked — zero real external LLM calls.
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AIPromptVersion, Column, Connection, Dataset, Issue, ReviewRun, Schema, User
from app.modules.ai.providers import ProviderResponse
from app.modules.ai.suggestion_service import AISuggestionService
from app.modules.approval.service import ApprovalService
from app.modules.review.decision_service import CorrectionDecisionService


def _create_prompt_version(db: Session, admin_user: User) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key="ai_correction", version_number=4, template="template", default_model="claude-test",
        is_active=True, created_by=admin_user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_correction_provider(suggested_value="fixed-value"):
    payload = json.dumps(
        {"category": "AI_HIGH_CONFIDENCE", "suggested_value": suggested_value, "confidence": 0.9, "reasoning": "n/a"}
    )
    fake_response = ProviderResponse(content=payload, input_tokens=10, output_tokens=5, latency_ms=5, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _build_draft_review_run(db, redis_client, admin_user, pg_connection, table_name):
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.review.service import ReviewService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'bad-email')"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()

    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"p410_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="PATTERN", origin="CUSTOM", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        severity="MEDIUM", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
    )

    validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name=f"p410_{uuid.uuid4().hex[:6]}", actor=admin_user
    )
    db.expire_all()
    return review_run


def test_generate_corrections_transitions_draft_review_run_to_in_review(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch,
) -> None:
    table_name = f"dq_p410_lc_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_draft_review_run(db, redis_client, admin_user, pg_connection, table_name)
        assert review_run.status == "DRAFT"
        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_correction_provider()}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        refreshed = db.get(ReviewRun, review_run.id)
        assert refreshed.status == "IN_REVIEW"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_generate_corrections_on_already_in_review_run_is_idempotent(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch,
) -> None:
    """Calling generate_corrections again on an already-IN_REVIEW run
    (e.g. a second AI-suggestions pass) must never regress the status or
    error — mirrors generate_for_review_run's own no-op-when-already-past-
    DRAFT guard."""
    table_name = f"dq_p410_idem_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_draft_review_run(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_correction_provider()}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()
        assert db.get(ReviewRun, review_run.id).status == "IN_REVIEW"

        # Second call: no remaining PENDING/unsuggested issues, must not raise.
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_correction_provider()}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()
        assert db.get(ReviewRun, review_run.id).status == "IN_REVIEW"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_ai_only_review_run_can_reach_submit_for_approval(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch,
) -> None:
    """The real end-to-end proof: a review run whose ONLY review action is
    'Generate AI Suggestions' (never the deterministic generate-suggestions
    endpoint) must be able to reach ApprovalService.submit() once every
    issue is decided — this is the exact real-world scenario that was
    stuck at 'Review run <id> is DRAFT, not IN_REVIEW'."""
    table_name = f"dq_p410_submit_{uuid.uuid4().hex[:8]}"
    try:
        review_run = _build_draft_review_run(db, redis_client, admin_user, pg_connection, table_name)
        _create_prompt_version(db, admin_user)
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_correction_provider("fixed@example.com")}):
            AISuggestionService(db).generate_corrections(review_run.id, admin_user)
        db.expire_all()

        from app.db.models import CorrectionSuggestion

        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalar_one()
        suggestion = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)
        ).scalar_one()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        approval_request = ApprovalService(db).submit(review_run.id, admin_user)
        assert approval_request.status == "PENDING"
        db.expire_all()
        assert db.get(ReviewRun, review_run.id).status == "READY_FOR_APPROVAL"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

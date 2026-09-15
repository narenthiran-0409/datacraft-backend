"""Phase 4.6 — record_ref safety across a business-key confirmation.

Reproduces, against the real local Postgres connection, the exact
"IMPORTANT RECORD_REF LIMITATION CHECK" scenario from the Phase 4.6 spec:
a validation run executes while a dataset has no reliable key
(ROW_INDEX_FALLBACK), a business key is confirmed afterward, and the
OLD run's Issue.record_ref values (ROWIDX:N) must never be reinterpreted
as if they were declared-key values. Only a FRESH validation run,
executed after confirmation, produces record_ref values the Phase 4.7
fetch_rows_by_keys() path can safely consume.
"""
import json
import uuid
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    AIPromptVersion,
    Column,
    Connection,
    CorrectionSuggestion,
    Dataset,
    Issue,
    Schema,
    User,
)
from app.modules.ai.providers import ProviderResponse
from app.modules.ai.suggestion_service import AISuggestionService
from app.modules.discovery.tasks import run_discovery
from app.modules.jobs.service import JobsService
from app.modules.review.service import ReviewService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _mock_llm(payload: dict):
    from unittest.mock import MagicMock

    fake_response = ProviderResponse(
        content=json.dumps(payload), input_tokens=10, output_tokens=10, latency_ms=5, raw_metadata={}
    )
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def _validate_and_review(db: Session, admin_user: User, dataset: Dataset, name: str):
    validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()
    review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name=name, actor=admin_user)
    db.expire_all()
    return validation_run, review_run


def test_business_key_confirmation_makes_old_issues_stale_and_fresh_validation_reuses_phase47_path(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch,
) -> None:
    table_name = f"dq_bk_ref_{uuid.uuid4().hex[:8]}"
    try:
        # No PRIMARY KEY declared -> Discovery yields ROW_INDEX_FALLBACK,
        # exactly mirroring the real Customer_Orders shape.
        db.execute(text(f"CREATE TABLE {table_name} (order_no INT, customer_name TEXT, email TEXT)"))
        db.execute(
            text(
                f"INSERT INTO {table_name} VALUES "
                "(1,'Arun Kumar','arun.kumar@gmail.com'),"
                "(2,'Priya Raj','priya.raj@gmail.com'),"
                "(3,'Vijay Kumar','vijay.kumar@gmail.com'),"
                "(4,'Meena Devi','meena.devi@gmail.com'),"
                "(5,'David Wilson','david.wilson@gmail.com'),"
                "(6,'Anitha Raj','anitha.raj@gmail.com'),"
                "(7,'Ramesh Babu','ramesh.babu@gmail.com'),"
                "(8,'John Smith','john.smith@gmail.com'),"
                "(9,'Suresh Kumar','suresh-invalid-email'),"
                "(10,'Lakshmi Priya','lakshmi.priya@gmail.com')"
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
        assert dataset.key_strategy == "ROW_INDEX_FALLBACK"

        email_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")).scalar_one()
        rules_service = RulesService(db)
        rule = rules_service.create_rule(
            actor=admin_user, name=f"bk_ref_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="PATTERN", origin="CUSTOM", definition={"regex": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
            severity="MEDIUM", error_message_template=None,
        )
        version = rules_service.list_versions(rule.id)[0]
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=email_col.id, column_ids=None, template_id=None,
        )

        # --- OLD validation run, while the dataset is still ROW_INDEX_FALLBACK ---
        old_validation_run, old_review_run = _validate_and_review(db, admin_user, dataset, "before_key_confirm")
        old_issue = db.execute(
            select(Issue).where(Issue.review_run_id == old_review_run.id, Issue.column_id == email_col.id)
        ).scalar_one()
        assert old_issue.record_ref.startswith("ROWIDX:")

        prompt_version = AIPromptVersion(
            prompt_key="ai_correction", version_number=1, template="template", default_model="claude-test",
            is_active=True, created_by=admin_user.id,
        )
        db.add(prompt_version)
        db.commit()
        monkeypatch.setattr(settings, "AI_ENABLED", True)
        monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
        monkeypatch.setattr(settings, "AI_CORRECTION_ADVANCED_INFERENCE_ENABLED", True)

        payload = {
            "category": "NEEDS_REVIEW", "suggested_value": None, "confidence": None,
            "reasoning": "n/a",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload)}):
            AISuggestionService(db).generate_corrections(old_review_run.id, admin_user)
        db.expire_all()

        old_bridge = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == old_issue.id)
        ).scalar_one()
        assert old_bridge.evidence_detail is None
        from app.db.models import AISuggestion

        old_ai_suggestion = db.get(AISuggestion, old_bridge.ai_suggestion_id)
        assert old_ai_suggestion.content["advanced_evidence"]["available"] is False
        assert old_ai_suggestion.content["advanced_evidence"]["reason"] == (
            "no_reliable_key_strategy_for_targeted_row_retrieval"
        )

        # --- confirm the business key (order_no is genuinely unique/non-null) ---
        from app.modules.datasets.business_key_service import BusinessKeyService
        from app.modules.connections.credential_vault import LocalRedisVaultClient

        vault = LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY)
        confirmed_dataset = BusinessKeyService(db, vault, redis_client).confirm(
            actor=admin_user, dataset_id=dataset.id, columns=["order_no"]
        )
        assert confirmed_dataset.key_strategy == "SINGLE_COLUMN"

        # The OLD issue's record_ref must be treated as stale now — never
        # reinterpreted as an order_no value.
        stale = AISuggestionService(db)._key_config_changed_since_validation_run(
            issue=old_issue, dataset_id=dataset.id
        )
        assert stale is True

        # --- a FRESH validation run, AFTER confirmation ---
        new_validation_run, new_review_run = _validate_and_review(db, admin_user, dataset, "after_key_confirm")
        new_issue = db.execute(
            select(Issue).where(Issue.review_run_id == new_review_run.id, Issue.column_id == email_col.id)
        ).scalar_one()
        # order_no=9 is Suresh's row -> the new record_ref must be the real key value.
        assert new_issue.record_ref == "9"

        payload2 = {
            "category": "AI_HIGH_CONFIDENCE", "suggested_value": "suresh.kumar@gmail.com", "confidence": 0.9,
            "reasoning": "Matches the consistent firstname.lastname@gmail.com template.",
        }
        with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_llm(payload2)}):
            AISuggestionService(db).generate_corrections(new_review_run.id, admin_user)
        db.expire_all()

        new_bridge = db.execute(
            select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == new_issue.id)
        ).scalar_one()
        new_ai_suggestion = db.get(AISuggestion, new_bridge.ai_suggestion_id)
        # Proof the Phase 4.7 fetch_rows_by_keys() path was reached and
        # actually used the newly-confirmed key — no new retrieval mechanism.
        assert new_ai_suggestion.content["advanced_evidence"]["available"] is True
        assert new_bridge.suggested_value == "suresh.kumar@gmail.com"
        assert new_bridge.strategy == "STRING_TEMPLATE"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

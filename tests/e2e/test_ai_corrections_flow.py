"""End-to-end: generate an AI CORRECTION suggestion -> existing Phase 6
human acceptance -> existing Phase 7 approval -> existing Phase 8 staging
-> existing Phase 9 publishing, through the real HTTP API with Celery in
eager mode. Provider always mocked — zero real external LLM calls.
Confirms the existing workflow remained fully authoritative and unchanged
at every step."""
import uuid
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
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


def _mock_provider_cls(text_content: str):
    fake_response = ProviderResponse(content=text_content, input_tokens=1, output_tokens=1, latency_ms=1, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


def test_ai_correction_flows_through_unchanged_phase6_9_pipeline(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path, monkeypatch
) -> None:
    table_name = f"dq_e2e_ai_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(tmp_path / "exports"))
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
            db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, NULL)"))
            db.execute(text(f"ANALYZE {table_name}"))
            db.commit()

            from app.modules.discovery.tasks import run_discovery
            from app.modules.jobs.service import JobsService

            discover_job = JobsService(db, redis_client).create(
                job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
            )
            run_discovery(str(discover_job.id))
            db.expire_all()

            schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
            dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
            val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

            client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})

            rule_response = client.post(
                "/api/v1/rules", headers=admin_headers,
                json={"name": f"e2e_ai_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
            )
            version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
            client.post(
                "/api/v1/rule-assignments", headers=admin_headers,
                json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(val_col.id)},
            )

            validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
            validation_run_id = validate_response.json()["id"]

            review_response = client.post(
                "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e ai"}
            )
            review_id = review_response.json()["id"]

            # Seed the ai_correction prompt version (deferred seed-script
            # convention — done directly here for the test, mirroring how
            # every other Phase 12 test provisions one).
            prompt_version = AIPromptVersion(
                prompt_key="ai_correction", version_number=1, template="template", default_model="claude-test",
                is_active=True, created_by=admin_user.id,
            )
            db.add(prompt_version)
            db.commit()

            # DRAFT -> IN_REVIEW is normally triggered by generate-suggestions
            # (Phase 6/7's own established transition point), but that would
            # also generate a competing RULE_BASED suggestion for the same
            # single issue, pre-empting the AI suggestion this test exists to
            # exercise. Direct status update instead — the same sanctioned
            # test-only workaround already used by Phase 8/9's own test
            # suites for this exact "no other DRAFT->IN_REVIEW API path"
            # limitation.
            db.execute(text("UPDATE review_runs SET status = 'IN_REVIEW' WHERE id = :rid"), {"rid": uuid.UUID(review_id)})
            db.commit()

            # AI generates a CORRECTION suggestion (async endpoint, eager Celery).
            with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider_cls("ai-corrected-value")}):
                ai_trigger = client.post(
                    "/api/v1/ai/suggestions/corrections", headers=admin_headers, json={"review_run_id": review_id}
                )
            assert ai_trigger.status_code == 202

            db.expire_all()
            bridge_row = db.execute(
                select(CorrectionSuggestion)
                .join(Issue, Issue.id == CorrectionSuggestion.issue_id)
                .where(Issue.review_run_id == uuid.UUID(review_id))
            ).scalars().first()
            assert bridge_row is not None
            assert bridge_row.source == "AI"
            assert bridge_row.suggested_value == "ai-corrected-value"
            issue_id = str(bridge_row.issue_id)

            # Existing, UNMODIFIED Phase 6 acceptance endpoint.
            accept_response = client.post(f"/api/v1/suggestions/{bridge_row.id}/accept", headers=admin_headers, json={})
            assert accept_response.status_code == 200
            assert accept_response.json()["final_value"] == "ai-corrected-value"

            # Existing, UNMODIFIED Phase 7 approval.
            submit_response = client.post(f"/api/v1/reviews/{review_id}/submit-approval", headers=admin_headers, json={})
            assert submit_response.status_code == 201
            approval_id = submit_response.json()["id"]
            approve_response = client.post(
                f"/api/v1/approvals/{approval_id}/approve", headers=admin_headers, json={"issue_ids": [issue_id]}
            )
            assert approve_response.status_code == 200
            assert approve_response.json()["status"] == "APPROVED"

            # Existing, UNMODIFIED Phase 8 staging.
            staging_response = client.post(f"/api/v1/reviews/{review_id}/staging", headers=admin_headers, json={})
            assert staging_response.status_code == 201
            staging_run_id = staging_response.json()["id"]
            assert staging_response.json()["status"] == "READY"

            records_response = client.get(f"/api/v1/staging-runs/{staging_run_id}/records", headers=admin_headers)
            assert records_response.json()[0]["row_snapshot"]["val"] == "ai-corrected-value"

            # Existing, UNMODIFIED Phase 9 publishing.
            publish_response = client.post(
                f"/api/v1/staging-runs/{staging_run_id}/publish", headers=admin_headers,
                json={"target_type": "FILE_EXPORT", "target_reference": "e2e_ai_out.jsonl"},
            )
            assert publish_response.status_code == 202
            publish_run_id = publish_response.json()["publish_run_id"]

            get_publish = client.get(f"/api/v1/publish-runs/{publish_run_id}", headers=admin_headers)
            assert get_publish.status_code == 200
            assert get_publish.json()["status"] == "PUBLISHED"

            output_path = tmp_path / "exports" / "e2e_ai_out.jsonl"
            assert output_path.exists()
            assert "ai-corrected-value" in output_path.read_text(encoding="utf-8")
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

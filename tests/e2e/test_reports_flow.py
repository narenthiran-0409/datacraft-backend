"""End-to-end: a realistic pipeline (validate -> review -> correction ->
approval, with a deliberate mix of accept/reject outcomes) driven through
the real HTTP API, then all 5 Reports endpoints are called and every
returned number is compared against direct database truth.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import ApprovalRequest, Column, Connection, Correction, Dataset, Schema, User, ValidationRun


def test_full_pipeline_reports_match_database_truth(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_reports_{uuid.uuid4().hex[:8]}"
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'x'), (2, 'x'), (3, NULL), (4, NULL)"))
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
            json={"name": f"e2e_reports_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
        )
        version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
        client.post(
            "/api/v1/rule-assignments", headers=admin_headers,
            json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(val_col.id)},
        )

        validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
        validation_run_id = validate_response.json()["id"]
        db.expire_all()
        validation_run = db.get(ValidationRun, uuid.UUID(validation_run_id))
        expected_quality_score = validation_run.quality_score
        assert expected_quality_score == Decimal("50.00")  # 2 passed / 4 total

        review_response = client.post(
            "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e reports"}
        )
        review_id = review_response.json()["id"]
        client.post(f"/api/v1/reviews/{review_id}/generate-suggestions", headers=admin_headers, json={})

        suggestions = client.get(f"/api/v1/reviews/{review_id}/suggestions", headers=admin_headers).json()
        assert len(suggestions) == 2

        # Mixed outcomes: accept the first, reject the second.
        accept_response = client.post(f"/api/v1/suggestions/{suggestions[0]['id']}/accept", headers=admin_headers, json={})
        assert accept_response.status_code == 200
        client.post(f"/api/v1/suggestions/{suggestions[1]['id']}/reject", headers=admin_headers, json={"reason": "not applicable"})

        submit_response = client.post(f"/api/v1/reviews/{review_id}/submit-approval", headers=admin_headers, json={})
        approval_id = submit_response.json()["id"]
        accepted_issue_id = suggestions[0]["issue_id"]
        approve_response = client.post(
            f"/api/v1/approvals/{approval_id}/approve", headers=admin_headers, json={"issue_ids": [accepted_issue_id]}
        )
        assert approve_response.status_code == 200
        db.expire_all()

        from_param = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        to_param = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        # --- quality-trend ---
        trend = client.get(
            "/api/v1/reports/quality-trend", headers=admin_headers,
            params={"dataset_id": str(dataset.id), "from": from_param, "to": to_param},
        )
        assert trend.status_code == 200
        points = trend.json()["points"]
        assert len(points) == 1
        assert Decimal(points[0]["quality_score"]) == expected_quality_score

        # --- quality-by-dataset ---
        qbd = client.get("/api/v1/reports/quality-by-dataset", headers=admin_headers)
        assert qbd.status_code == 200
        row = next(r for r in qbd.json()["datasets"] if r["dataset_id"] == str(dataset.id))
        assert Decimal(row["latest_quality_score"]) == expected_quality_score

        # --- rule-effectiveness ---
        re_response = client.get(
            "/api/v1/reports/rule-effectiveness", headers=admin_headers,
            params={"dataset_id": str(dataset.id), "from": from_param, "to": to_param},
        )
        assert re_response.status_code == 200
        rules = re_response.json()["rules"]
        assert len(rules) == 1
        assert rules[0]["failure_count"] == 2  # two NULLs
        for forbidden_key in ("failed_value", "original_value", "final_value", "corrected_fields", "row_snapshot"):
            assert forbidden_key not in rules[0]

        # --- review-performance ---
        rp_response = client.get(
            "/api/v1/reports/review-performance", headers=admin_headers, params={"from": from_param, "to": to_param}
        )
        assert rp_response.status_code == 200
        reviewers = rp_response.json()["reviewers"]
        db_decision_count = db.execute(
            select(Correction).where(Correction.decided_by == admin_user.id)
        ).scalars().all()
        reviewer_row = next(r for r in reviewers if r["reviewer_id"] == str(admin_user.id))
        assert reviewer_row["decision_count"] == len(db_decision_count)

        # --- approval-metrics ---
        am_response = client.get(
            "/api/v1/reports/approval-metrics", headers=admin_headers, params={"from": from_param, "to": to_param}
        )
        assert am_response.status_code == 200
        metrics = am_response.json()
        approval_request = db.execute(select(ApprovalRequest).where(ApprovalRequest.review_run_id == uuid.UUID(review_id))).scalar_one()
        assert metrics["total_requests"] == 1
        assert metrics["approved_count"] == (1 if approval_request.status == "APPROVED" else 0)

        # --- security: no raw values anywhere in any response body ---
        for response in (trend, qbd, re_response, rp_response, am_response):
            body_text = response.text
            assert "row_snapshot" not in body_text
            assert "failed_value" not in body_text
            assert "corrected_fields" not in body_text
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

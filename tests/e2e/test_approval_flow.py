"""End-to-end: validation -> review -> correct (Phase 6) -> submit for
approval -> partially approve -> reject the remainder -> confirm
approval_requests/approval_decisions/approval_decision_issues are
internally consistent at every step, and confirm the review run's
corrections were never modified by any of this. Mirrors
tests/e2e/test_review_flow.py's structure.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import Column, Connection, Correction, Dataset, Schema, User


def test_full_approval_flow(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_approval_{uuid.uuid4().hex[:8]}"
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, a TEXT, b TEXT)"))
            db.execute(
                text(
                    f"INSERT INTO {table_name} VALUES "
                    "(1, 'x', 'y'), (2, 'x', 'y'), (3, NULL, 'y'), (4, 'x', NULL)"
                )
            )
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

            profile_response = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})
            assert profile_response.status_code == 202

            col_a = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
            col_b = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "b")).scalar_one()

            for col in (col_a, col_b):
                rule_response = client.post(
                    "/api/v1/rules", headers=admin_headers,
                    json={"name": f"e2e_approval_{col.name}_{uuid.uuid4().hex[:6]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
                )
                assert rule_response.status_code == 201
                version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
                assignment_response = client.post(
                    "/api/v1/rule-assignments", headers=admin_headers,
                    json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(col.id)},
                )
                assert assignment_response.status_code == 201

            validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
            assert validate_response.status_code == 202
            validation_run_id = validate_response.json()["id"]

            review_response = client.post(
                "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e approval"}
            )
            assert review_response.status_code == 201
            review_id = review_response.json()["id"]

            generate_response = client.post(f"/api/v1/reviews/{review_id}/generate-suggestions", headers=admin_headers, json={})
            assert generate_response.status_code == 200

            review_after_generate = client.get(f"/api/v1/reviews/{review_id}", headers=admin_headers)
            assert review_after_generate.json()["status"] == "IN_REVIEW"  # the Phase 7 fix, verified end-to-end

            suggestions = client.get(f"/api/v1/reviews/{review_id}/suggestions", headers=admin_headers).json()
            assert len(suggestions) == 2  # both NULL cells are mode_fill-able (categorical text columns)

            resolved_issue_ids = []
            for suggestion in suggestions:
                accept_response = client.post(f"/api/v1/suggestions/{suggestion['id']}/accept", headers=admin_headers, json={})
                assert accept_response.status_code == 200
                resolved_issue_ids.append(suggestion["issue_id"])

            # Snapshot corrections before submitting/deciding, to later prove
            # approval never touches them.
            corrections_before = {
                str(c.issue_id): (c.final_value, c.status)
                for c in db.execute(select(Correction).where(Correction.issue_id.in_(resolved_issue_ids))).scalars()
            }

            submit_response = client.post(f"/api/v1/reviews/{review_id}/submit-approval", headers=admin_headers, json={})
            assert submit_response.status_code == 201
            approval_id = submit_response.json()["id"]
            assert submit_response.json()["status"] == "PENDING"
            assert submit_response.json()["affected_issue_count"] == 2

            review_after_submit = client.get(f"/api/v1/reviews/{review_id}", headers=admin_headers)
            assert review_after_submit.json()["status"] == "READY_FOR_APPROVAL"

            # Partially approve: only the first issue.
            approve_response = client.post(
                f"/api/v1/approvals/{approval_id}/approve", headers=admin_headers,
                json={"issue_ids": [resolved_issue_ids[0]], "comment": "first one looks right"},
            )
            assert approve_response.status_code == 200
            assert approve_response.json()["status"] == "PARTIALLY_APPROVED"

            detail_response = client.get(f"/api/v1/approvals/{approval_id}", headers=admin_headers)
            assert detail_response.json()["decided_count"] == 1
            assert detail_response.json()["remaining_count"] == 1

            # Reject the remainder.
            reject_response = client.post(
                f"/api/v1/approvals/{approval_id}/reject", headers=admin_headers,
                json={"issue_ids": [resolved_issue_ids[1]], "comment": "needs another look"},
            )
            assert reject_response.status_code == 200
            assert reject_response.json()["status"] == "REJECTED"  # mixed outcome -> REJECTED overall

            final_detail = client.get(f"/api/v1/approvals/{approval_id}", headers=admin_headers)
            assert final_detail.json()["decided_count"] == 2
            assert final_detail.json()["remaining_count"] == 0

            # Internal consistency: exactly 2 approval_decisions rows, each
            # with exactly 1 approval_decision_issues row, covering the 2
            # distinct resolved issues with no overlap.
            from app.db.models import ApprovalDecision, ApprovalDecisionIssue

            decisions = db.execute(
                select(ApprovalDecision).where(ApprovalDecision.approval_request_id == approval_id)
            ).scalars().all()
            assert len(decisions) == 2
            assert {d.decision for d in decisions} == {"APPROVE", "REJECT"}

            decision_issue_rows = db.execute(
                select(ApprovalDecisionIssue).join(
                    ApprovalDecision, ApprovalDecision.id == ApprovalDecisionIssue.approval_decision_id
                ).where(ApprovalDecision.approval_request_id == approval_id)
            ).scalars().all()
            assert len(decision_issue_rows) == 2
            assert {str(r.issue_id) for r in decision_issue_rows} == set(resolved_issue_ids)

            # Corrections were NEVER modified by any Phase 7 action.
            corrections_after = {
                str(c.issue_id): (c.final_value, c.status)
                for c in db.execute(select(Correction).where(Correction.issue_id.in_(resolved_issue_ids))).scalars()
            }
            assert corrections_before == corrections_after
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

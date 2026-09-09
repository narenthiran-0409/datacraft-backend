"""End-to-end: validation -> review -> correction -> approval -> APPROVED
-> staging -> publish (FILE_EXPORT) -> verify output file content, plus the
drift-blocked-then-acknowledged-then-published lifecycle, all through the
real HTTP API with Celery in eager mode. Mirrors
tests/e2e/test_staging_flow.py's structure.
"""
import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.models import (
    ApprovalDecision,
    ApprovalDecisionIssue,
    ApprovalRequest,
    Column,
    Connection,
    Correction,
    Dataset,
    Schema,
    StagingRecord,
    StagingRun,
    User,
)


def _run_pipeline_to_staged(client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, value: str):
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, '{value}'), (2, '{value}'), (3, NULL)"))
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

    profile_response = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})
    assert profile_response.status_code == 202

    rule_response = client.post(
        "/api/v1/rules", headers=admin_headers,
        json={"name": f"e2e_pub_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
    )
    assert rule_response.status_code == 201
    version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
    assignment_response = client.post(
        "/api/v1/rule-assignments", headers=admin_headers,
        json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(val_col.id)},
    )
    assert assignment_response.status_code == 201

    validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
    assert validate_response.status_code == 202
    validation_run_id = validate_response.json()["id"]

    review_response = client.post(
        "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e publishing"}
    )
    assert review_response.status_code == 201
    review_id = review_response.json()["id"]

    generate_response = client.post(f"/api/v1/reviews/{review_id}/generate-suggestions", headers=admin_headers, json={})
    assert generate_response.status_code == 200

    suggestions = client.get(f"/api/v1/reviews/{review_id}/suggestions", headers=admin_headers).json()
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    issue_id = suggestion["issue_id"]

    accept_response = client.post(f"/api/v1/suggestions/{suggestion['id']}/accept", headers=admin_headers, json={})
    assert accept_response.status_code == 200

    submit_response = client.post(f"/api/v1/reviews/{review_id}/submit-approval", headers=admin_headers, json={})
    assert submit_response.status_code == 201
    approval_id = submit_response.json()["id"]

    approve_response = client.post(
        f"/api/v1/approvals/{approval_id}/approve", headers=admin_headers, json={"issue_ids": [issue_id]}
    )
    assert approve_response.status_code == 200

    staging_response = client.post(f"/api/v1/reviews/{review_id}/staging", headers=admin_headers, json={})
    assert staging_response.status_code == 201
    staging_run_id = staging_response.json()["id"]
    assert staging_response.json()["status"] == "READY"

    return staging_run_id, issue_id


def test_full_publishing_flow(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path, monkeypatch
) -> None:
    table_name = f"dq_e2e_publish_{uuid.uuid4().hex[:8]}"
    export_dir = tmp_path / "publish_exports"
    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(export_dir))
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            staging_run_id, issue_id = _run_pipeline_to_staged(
                client, admin_headers, db, redis_client, admin_user, pg_connection, table_name, "e2eval"
            )

            corrections_before = list(db.execute(select(Correction)).scalars())
            approval_rows_before = (
                len(db.execute(select(ApprovalRequest)).scalars().all()),
                len(db.execute(select(ApprovalDecision)).scalars().all()),
                len(db.execute(select(ApprovalDecisionIssue)).scalars().all()),
            )
            staging_runs_before = {r.id: (r.status, r.is_current) for r in db.execute(select(StagingRun)).scalars()}
            staging_records_before = {r.id: r.row_snapshot for r in db.execute(select(StagingRecord)).scalars()}

            publish_response = client.post(
                f"/api/v1/staging-runs/{staging_run_id}/publish", headers=admin_headers,
                json={"target_type": "FILE_EXPORT", "target_reference": "e2e_out.jsonl"},
            )
            assert publish_response.status_code == 202
            publish_run_id = publish_response.json()["publish_run_id"]

            get_response = client.get(f"/api/v1/publish-runs/{publish_run_id}", headers=admin_headers)
            assert get_response.status_code == 200
            body = get_response.json()
            assert body["status"] == "PUBLISHED"
            assert body["published_record_count"] == 1
            assert body["target_type"] == "FILE_EXPORT"

            output_path = export_dir / "e2e_out.jsonl"
            assert output_path.exists()
            rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").strip().split("\n")]
            assert len(rows) == 1
            assert rows[0]["val"] == "e2eval"
            assert rows[0]["id"] == 3

            # Nothing in Phase 1-8 tables was modified by publishing.
            corrections_after = list(db.execute(select(Correction)).scalars())
            assert [(c.id, c.final_value, c.status) for c in corrections_before] == [
                (c.id, c.final_value, c.status) for c in corrections_after
            ]
            approval_rows_after = (
                len(db.execute(select(ApprovalRequest)).scalars().all()),
                len(db.execute(select(ApprovalDecision)).scalars().all()),
                len(db.execute(select(ApprovalDecisionIssue)).scalars().all()),
            )
            assert approval_rows_before == approval_rows_after
            staging_runs_after = {r.id: (r.status, r.is_current) for r in db.execute(select(StagingRun)).scalars()}
            assert staging_runs_before == staging_runs_after
            staging_records_after = {r.id: r.row_snapshot for r in db.execute(select(StagingRecord)).scalars()}
            assert staging_records_before == staging_records_after
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_drift_blocked_publish_completes_after_acknowledge(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path, monkeypatch
) -> None:
    table_name = f"dq_e2e_publish_drift_{uuid.uuid4().hex[:8]}"
    export_dir = tmp_path / "publish_exports"
    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(export_dir))
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            staging_run_id, _ = _run_pipeline_to_staged(
                client, admin_headers, db, redis_client, admin_user, pg_connection, table_name, "driftval"
            )

            db.execute(text("UPDATE staging_runs SET has_source_drift = true WHERE id = :sid"), {"sid": staging_run_id})
            db.commit()

            publish_response = client.post(
                f"/api/v1/staging-runs/{staging_run_id}/publish", headers=admin_headers,
                json={"target_type": "FILE_EXPORT", "target_reference": "drift_out.jsonl"},
            )
            assert publish_response.status_code == 202
            publish_run_id = publish_response.json()["publish_run_id"]
            job_id = publish_response.json()["job_id"]

            # task_always_eager means the trigger's dispatched task already
            # ran synchronously and hit the drift gate — still PENDING.
            blocked = client.get(f"/api/v1/publish-runs/{publish_run_id}", headers=admin_headers)
            assert blocked.json()["status"] == "PENDING"
            assert not (export_dir / "drift_out.jsonl").exists()

            ack_response = client.post(
                f"/api/v1/publish-runs/{publish_run_id}/drift-acknowledge", headers=admin_headers,
                json={"comment": "acknowledged for e2e test"},
            )
            assert ack_response.status_code == 200
            ack_body = ack_response.json()
            assert ack_body["drift_acknowledged"] is True
            assert ack_body["id"] == publish_run_id  # same row reused
            assert ack_body["job_id"] == job_id  # same job reused
            assert ack_body["status"] == "PUBLISHED"  # eager mode: re-enqueue ran synchronously

            output_path = export_dir / "drift_out.jsonl"
            assert output_path.exists()
            rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").strip().split("\n")]
            assert rows[0]["val"] == "driftval"
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

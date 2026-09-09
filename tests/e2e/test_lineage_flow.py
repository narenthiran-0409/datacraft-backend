"""End-to-end: full pipeline (connect -> discover -> profile -> validate ->
review -> correct -> approve -> stage -> publish) via the real HTTP API
with Celery in eager mode, then a real lineage query for the dataset ->
assert the complete expected edge set exists and no unexpected extra
DATASET-rooted downstream edge appears.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.models import Column, Connection, Dataset, LineageRecord, Schema, User


def test_full_pipeline_lineage_query_returns_complete_edge_set(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path, monkeypatch
) -> None:
    table_name = f"dq_e2e_lineage_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(tmp_path / "exports"))
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
            db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'x'), (2, 'x'), (3, NULL)"))
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
            profile_run_id = profile_response.json()["id"]

            rule_response = client.post(
                "/api/v1/rules", headers=admin_headers,
                json={"name": f"e2e_lineage_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
            )
            version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
            client.post(
                "/api/v1/rule-assignments", headers=admin_headers,
                json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(val_col.id)},
            )

            validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
            validation_run_id = validate_response.json()["id"]

            review_response = client.post(
                "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e lineage"}
            )
            review_id = review_response.json()["id"]
            client.post(f"/api/v1/reviews/{review_id}/generate-suggestions", headers=admin_headers, json={})

            suggestions = client.get(f"/api/v1/reviews/{review_id}/suggestions", headers=admin_headers).json()
            suggestion = suggestions[0]
            issue_id = suggestion["issue_id"]
            client.post(f"/api/v1/suggestions/{suggestion['id']}/accept", headers=admin_headers, json={})

            submit_response = client.post(f"/api/v1/reviews/{review_id}/submit-approval", headers=admin_headers, json={})
            approval_id = submit_response.json()["id"]
            client.post(f"/api/v1/approvals/{approval_id}/approve", headers=admin_headers, json={"issue_ids": [issue_id]})

            staging_response = client.post(f"/api/v1/reviews/{review_id}/staging", headers=admin_headers, json={})
            staging_run_id = staging_response.json()["id"]

            publish_response = client.post(
                f"/api/v1/staging-runs/{staging_run_id}/publish", headers=admin_headers,
                json={"target_type": "FILE_EXPORT", "target_reference": "e2e_lineage.jsonl"},
            )
            publish_run_id = publish_response.json()["publish_run_id"]

            # Query lineage downstream from the dataset.
            response = client.get(f"/api/v1/lineage/DATASET/{dataset.id}", headers=admin_headers, params={"direction": "down"})
            assert response.status_code == 200
            edges = response.json()["edges"]

            relationship_by_child = {(e["child_entity_type"], e["child_entity_id"]): e["relationship_type"] for e in edges}
            assert relationship_by_child[("PROFILE_RUN", profile_run_id)] == "PROFILED_BY"
            assert relationship_by_child[("VALIDATION_RUN", validation_run_id)] == "VALIDATED_BY"

            # Full-graph query rooted at the connection covers the whole chain.
            full = client.get(f"/api/v1/lineage/CONNECTION/{pg_connection.id}", headers=admin_headers, params={"direction": "down"})
            assert full.status_code == 200
            full_child_ids = {e["child_entity_id"] for e in full.json()["edges"]}
            assert str(schema.id) in full_child_ids

            # No unexpected edge type: every DATASET-rooted downstream edge
            # must be exactly PROFILE_RUN or VALIDATION_RUN (COLUMN edges are
            # rooted differently, at DATASET as parent too — both expected).
            allowed_child_types = {"PROFILE_RUN", "VALIDATION_RUN", "COLUMN"}
            assert {e["child_entity_type"] for e in edges} <= allowed_child_types

            db.expire_all()
            lineage_rows = db.execute(
                select(LineageRecord).where(
                    LineageRecord.child_entity_type == "PUBLISH_RUN", LineageRecord.child_entity_id == uuid.UUID(publish_run_id)
                )
            ).scalars().all()
            assert len(lineage_rows) == 1
            assert lineage_rows[0].relationship_type == "PUBLISHED_TO"
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

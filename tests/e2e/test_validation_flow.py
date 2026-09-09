"""End-to-end: create rule via API -> assign to dataset via API -> trigger
validation via the real HTTP API -> real (eager) Celery execution -> poll
job to completion -> retrieve via GET /validation-runs/{id} and
GET /datasets/{id}/validation -> verify datasets.last_quality_score updated
-> verify audit trail. Mirrors tests/e2e/test_profiling_flow.py's structure.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import AuditEvent, Connection, Dataset, Schema, User


def _make_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str) -> Dataset:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, email TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a@x.com'), (2, NULL), (3, 'c@x.com')"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    return db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()


def test_full_validation_flow(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_validate_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name)

        columns_response = None
        from app.db.models import Column

        email_column = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "email")
        ).scalar_one()

        rule_response = client.post(
            "/api/v1/rules", headers=admin_headers,
            json={
                "name": f"email_not_null_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS",
                "definition": {"max_null_percentage": 0}, "severity": "HIGH",
            },
        )
        assert rule_response.status_code == 201
        rule_id = rule_response.json()["id"]

        versions_response = client.get(f"/api/v1/rules/{rule_id}/versions", headers=admin_headers)
        assert versions_response.status_code == 200
        version_id = versions_response.json()[0]["id"]

        assignment_response = client.post(
            "/api/v1/rule-assignments", headers=admin_headers,
            json={
                "rule_version_id": version_id, "dataset_id": str(dataset.id),
                "assignment_scope": "SINGLE_COLUMN", "column_id": str(email_column.id),
            },
        )
        assert assignment_response.status_code == 201

        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            start_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
            assert start_response.status_code == 202
            body = start_response.json()
            job_id = body["job_id"]
            validation_run_id = body["id"]

            job_response = client.get(f"/api/v1/jobs/{job_id}", headers=admin_headers)
            assert job_response.status_code == 200
            assert job_response.json()["status"] == "COMPLETED"

            run_by_id_response = client.get(f"/api/v1/validation-runs/{validation_run_id}", headers=admin_headers)
            assert run_by_id_response.status_code == 200
            run_body = run_by_id_response.json()
            assert run_body["status"] == "COMPLETED"
            assert run_body["total_rows"] == 3
            assert run_body["failed_rows"] == 1  # the NULL email row
            assert run_body["passed_rows"] == 2
            assert float(run_body["quality_score"]) == round(2 / 3 * 100, 2)

            latest_response = client.get(f"/api/v1/datasets/{dataset.id}/validation", headers=admin_headers)
            assert latest_response.status_code == 200
            assert latest_response.json()["id"] == validation_run_id

            dataset_response = client.get(f"/api/v1/datasets/{dataset.id}", headers=admin_headers)
            assert dataset_response.status_code == 200
            assert float(dataset_response.json()["last_quality_score"]) == round(2 / 3 * 100, 2)

            audit_rows = db.execute(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "DATASET", AuditEvent.entity_id == dataset.id,
                    AuditEvent.action.in_(("validation.started", "validation.completed")),
                )
            ).scalars().all()
            assert {a.action for a in audit_rows} == {"validation.started", "validation.completed"}
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

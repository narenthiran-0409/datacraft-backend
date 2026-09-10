"""Integration tests for GET /validation-runs/{id}/failures, against a real
local Postgres table (via pg_connection/db fixtures), mirroring
tests/integration/test_validation.py's structure and conventions.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Column, Connection, Dataset, RuleAssignment, Schema, User, ValidationRun
from app.modules.jobs.service import JobsService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _make_dataset(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, rows_sql: str) -> Dataset:
    from app.modules.discovery.tasks import run_discovery

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT, score NUMERIC)"))
    if rows_sql:
        db.execute(text(rows_sql))
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


def _assign_rule(
    db: Session, admin_user: User, dataset: Dataset, *, rule_type: str, definition: dict, column_id=None,
) -> RuleAssignment:
    rules_service = RulesService(db)
    rule = rules_service.create_rule(
        actor=admin_user, name=f"rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type=rule_type, origin="CUSTOM", definition=definition, severity="HIGH", error_message_template=None,
    )
    version = rules_service.list_versions(rule.id)[0]
    assignment_service = RuleAssignmentService(db)
    return assignment_service.create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=column_id, column_ids=None, template_id=None,
    )


def _run_completeness_failure(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str):
    dataset = _make_dataset(
        db, redis_client, admin_user, pg_connection, table_name,
        rows_sql=f"INSERT INTO {table_name} VALUES (1,NULL,1),(2,NULL,2),(3,'x',3)",
    )
    val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
    assignment = _assign_rule(
        db, admin_user, dataset, rule_type="COMPLETENESS", definition={"max_null_percentage": 0}, column_id=val_col.id
    )

    validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(job.id), str(validation_run.id))
    db.expire_all()

    completed_run = db.get(ValidationRun, validation_run.id)
    return dataset, val_col, assignment, completed_run


def test_list_failures_returns_joined_rule_and_column_names(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_valfail_{uuid.uuid4().hex[:8]}"
    try:
        dataset, val_col, assignment, completed_run = _run_completeness_failure(
            db, redis_client, admin_user, pg_connection, table_name
        )
        assert completed_run.failed_rows == 2

        response = client.get(f"/api/v1/validation-runs/{completed_run.id}/failures", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()

        assert body["total"] == 2
        assert body["page"] == 1
        assert body["page_size"] == 50
        assert len(body["items"]) == 2

        item = body["items"][0]
        assert item["validation_run_id"] == str(completed_run.id)
        assert item["rule_assignment_id"] == str(assignment.id)
        assert item["column_id"] == str(val_col.id)
        assert item["column_name"] == "val"
        assert item["assignment_scope"] == "SINGLE_COLUMN"
        assert item["severity"] == "HIGH"
        assert item["expected_value"] == "NOT NULL"
        assert item["failed_value"] is None
        assert "is null" in item["reason"]
        assert item["record_ref"]  # non-empty, joined from validation_results
        assert item["rule_name"]  # joined from rules via rule_versions
        assert item["rule_type"] == "COMPLETENESS"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_list_failures_paginates(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_valfail_page_{uuid.uuid4().hex[:8]}"
    try:
        _, _, _, completed_run = _run_completeness_failure(db, redis_client, admin_user, pg_connection, table_name)

        page1 = client.get(
            f"/api/v1/validation-runs/{completed_run.id}/failures",
            headers=admin_headers, params={"page": 1, "page_size": 1},
        ).json()
        page2 = client.get(
            f"/api/v1/validation-runs/{completed_run.id}/failures",
            headers=admin_headers, params={"page": 2, "page_size": 1},
        ).json()

        assert page1["total"] == 2
        assert page2["total"] == 2
        assert len(page1["items"]) == 1
        assert len(page2["items"]) == 1
        assert page1["items"][0]["id"] != page2["items"][0]["id"]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_list_failures_filters_by_severity(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_valfail_sev_{uuid.uuid4().hex[:8]}"
    try:
        _, _, _, completed_run = _run_completeness_failure(db, redis_client, admin_user, pg_connection, table_name)

        matching = client.get(
            f"/api/v1/validation-runs/{completed_run.id}/failures",
            headers=admin_headers, params={"severity": "HIGH"},
        ).json()
        non_matching = client.get(
            f"/api/v1/validation-runs/{completed_run.id}/failures",
            headers=admin_headers, params={"severity": "LOW"},
        ).json()

        assert matching["total"] == 2
        assert non_matching["total"] == 0
        assert non_matching["items"] == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_list_failures_filters_by_column_id(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_valfail_col_{uuid.uuid4().hex[:8]}"
    try:
        dataset, val_col, _, completed_run = _run_completeness_failure(
            db, redis_client, admin_user, pg_connection, table_name
        )
        other_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "score")).scalar_one()

        matching = client.get(
            f"/api/v1/validation-runs/{completed_run.id}/failures",
            headers=admin_headers, params={"column_id": str(val_col.id)},
        ).json()
        non_matching = client.get(
            f"/api/v1/validation-runs/{completed_run.id}/failures",
            headers=admin_headers, params={"column_id": str(other_col.id)},
        ).json()

        assert matching["total"] == 2
        assert non_matching["total"] == 0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_list_failures_nonexistent_run_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/validation-runs/{uuid.uuid4()}/failures", headers=admin_headers)
    assert response.status_code == 404


def test_analyst_can_list_failures(
    client: TestClient,
    admin_headers: dict,
    analyst_headers: dict,
    db: Session,
    redis_client,
    admin_user: User,
    pg_connection: Connection,
) -> None:
    """metadata.read (analyst has it) gates this route, same as every other
    validation-run read route — not validation.run, which only gates
    triggering a run."""
    table_name = f"dq_valfail_analyst_{uuid.uuid4().hex[:8]}"
    try:
        _, _, _, completed_run = _run_completeness_failure(db, redis_client, admin_user, pg_connection, table_name)

        response = client.get(f"/api/v1/validation-runs/{completed_run.id}/failures", headers=analyst_headers)
        assert response.status_code == 200
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_list_failures_zero_failures_returns_empty_page(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_valfail_empty_{uuid.uuid4().hex[:8]}"
    try:
        dataset = _make_dataset(db, redis_client, admin_user, pg_connection, table_name, rows_sql="")
        validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()
        completed_run = db.get(ValidationRun, validation_run.id)

        response = client.get(f"/api/v1/validation-runs/{completed_run.id}/failures", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert body["items"] == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

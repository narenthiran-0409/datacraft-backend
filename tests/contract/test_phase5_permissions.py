import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import Column, Connection, Dataset, Rule, RuleVersion, Schema, User

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("POST", "/api/v1/rules", {"name": "x", "rule_type": "COMPLETENESS", "definition": {}}),
    ("GET", "/api/v1/rules", None),
    ("GET", f"/api/v1/rules/{RANDOM_ID}", None),
    ("PATCH", f"/api/v1/rules/{RANDOM_ID}", {}),
    ("POST", f"/api/v1/rules/{RANDOM_ID}/versions", {"definition": {}}),
    ("GET", f"/api/v1/rules/{RANDOM_ID}/versions", None),
    (
        "POST",
        "/api/v1/rule-assignments",
        {"rule_version_id": RANDOM_ID, "dataset_id": RANDOM_ID, "assignment_scope": "DATASET_LEVEL"},
    ),
    ("GET", "/api/v1/rule-assignments", None),
    ("DELETE", f"/api/v1/rule-assignments/{RANDOM_ID}", None),
    ("POST", f"/api/v1/datasets/{RANDOM_ID}/validate", {}),
    ("GET", "/api/v1/validation-runs", None),
    ("GET", f"/api/v1/validation-runs/{RANDOM_ID}", None),
    ("GET", f"/api/v1/datasets/{RANDOM_ID}/validation", None),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase5_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase5_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def dataset_with_rule(db: Session, admin_user: User, pg_connection: Connection):
    """A real discovered dataset with one published rule/version, for the
    200-valid-request contract cases."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    table_name = f"dq_contract5_{uuid.uuid4().hex[:8]}"
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, NULL)"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    from app.core.redis_client import get_redis_client

    redis_client = get_redis_client()
    jobs_service = JobsService(db, redis_client)
    discover_job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(discover_job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()

    from app.modules.rules.service import RulesService

    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"contract_rule_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 100}, severity="LOW",
        error_message_template=None,
    )
    version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()

    yield dataset, rule, version

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_post_rules_valid_request_returns_201(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/api/v1/rules", headers=admin_headers,
        json={"name": f"r_{uuid.uuid4().hex[:8]}", "rule_type": "PATTERN", "definition": {"regex": ".*"}},
    )
    assert response.status_code == 201


def test_post_rules_unsupported_rule_type_returns_422(client: TestClient, admin_headers: dict) -> None:
    response = client.post(
        "/api/v1/rules", headers=admin_headers,
        json={"name": f"r_{uuid.uuid4().hex[:8]}", "rule_type": "REFERENTIAL_INTEGRITY", "definition": {}},
    )
    assert response.status_code == 422


def test_get_rules_valid_request_returns_200(client: TestClient, admin_headers: dict, dataset_with_rule) -> None:
    response = client.get("/api/v1/rules", headers=admin_headers)
    assert response.status_code == 200


def test_get_rule_by_id_valid_request_returns_200(client: TestClient, admin_headers: dict, dataset_with_rule) -> None:
    _, rule, _ = dataset_with_rule
    response = client.get(f"/api/v1/rules/{rule.id}", headers=admin_headers)
    assert response.status_code == 200


def test_post_rule_assignment_valid_request_returns_201(
    client: TestClient, admin_headers: dict, db: Session, dataset_with_rule
) -> None:
    dataset, rule, version = dataset_with_rule
    response = client.post(
        "/api/v1/rule-assignments", headers=admin_headers,
        json={"rule_version_id": str(version.id), "dataset_id": str(dataset.id), "assignment_scope": "DATASET_LEVEL"},
    )
    assert response.status_code == 201


def test_delete_rule_assignment_valid_request_returns_200(
    client: TestClient, admin_headers: dict, db: Session, dataset_with_rule
) -> None:
    dataset, rule, version = dataset_with_rule
    create_response = client.post(
        "/api/v1/rule-assignments", headers=admin_headers,
        json={"rule_version_id": str(version.id), "dataset_id": str(dataset.id), "assignment_scope": "DATASET_LEVEL"},
    )
    assignment_id = create_response.json()["id"]
    response = client.delete(f"/api/v1/rule-assignments/{assignment_id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["is_enabled"] is False


def test_post_validate_valid_request_returns_202(
    client: TestClient, admin_headers: dict, db: Session, dataset_with_rule
) -> None:
    dataset, rule, version = dataset_with_rule
    client.post(
        "/api/v1/rule-assignments", headers=admin_headers,
        json={"rule_version_id": str(version.id), "dataset_id": str(dataset.id), "assignment_scope": "DATASET_LEVEL"},
    )
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    try:
        response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
        assert response.status_code == 202
    finally:
        celery_app.conf.task_always_eager = False
        celery_app.conf.task_eager_propagates = False


def test_get_validation_runs_valid_request_returns_200(
    client: TestClient, admin_headers: dict, db: Session, dataset_with_rule
) -> None:
    response = client.get("/api/v1/validation-runs", headers=admin_headers)
    assert response.status_code == 200


# --- rules.manage is administrator-only (migration 0017 correction) --------
# CUSTOM_EXPRESSION rules are code-execution-adjacent, so rule creation and
# versioning are gated more strictly than the rest of this project's
# metadata-management actions: analyst holds rules.read and
# rule_assignments.manage, but not rules.manage.


def test_analyst_cannot_create_rule_returns_403(client: TestClient, analyst_headers: dict) -> None:
    response = client.post(
        "/api/v1/rules", headers=analyst_headers,
        json={"name": f"r_{uuid.uuid4().hex[:8]}", "rule_type": "PATTERN", "definition": {"regex": ".*"}},
    )
    assert response.status_code == 403


def test_analyst_cannot_publish_rule_version_returns_403(
    client: TestClient, analyst_headers: dict, dataset_with_rule
) -> None:
    _, rule, _ = dataset_with_rule
    response = client.post(
        f"/api/v1/rules/{rule.id}/versions", headers=analyst_headers, json={"definition": {}},
    )
    assert response.status_code == 403


def test_admin_can_publish_rule_version_returns_201(client: TestClient, admin_headers: dict, dataset_with_rule) -> None:
    _, rule, _ = dataset_with_rule
    response = client.post(
        f"/api/v1/rules/{rule.id}/versions", headers=admin_headers, json={"definition": {"max_null_percentage": 50}},
    )
    assert response.status_code == 201


def test_analyst_can_list_rules_returns_200(client: TestClient, analyst_headers: dict, dataset_with_rule) -> None:
    response = client.get("/api/v1/rules", headers=analyst_headers)
    assert response.status_code == 200


def test_analyst_can_get_rule_by_id_returns_200(client: TestClient, analyst_headers: dict, dataset_with_rule) -> None:
    _, rule, _ = dataset_with_rule
    response = client.get(f"/api/v1/rules/{rule.id}", headers=analyst_headers)
    assert response.status_code == 200


def test_analyst_can_create_rule_assignment_returns_201(
    client: TestClient, analyst_headers: dict, dataset_with_rule
) -> None:
    dataset, _, version = dataset_with_rule
    response = client.post(
        "/api/v1/rule-assignments", headers=analyst_headers,
        json={"rule_version_id": str(version.id), "dataset_id": str(dataset.id), "assignment_scope": "DATASET_LEVEL"},
    )
    assert response.status_code == 201


def test_analyst_can_list_rule_assignments_returns_200(client: TestClient, analyst_headers: dict) -> None:
    response = client.get("/api/v1/rule-assignments", headers=analyst_headers)
    assert response.status_code == 200


def test_analyst_can_disable_rule_assignment_returns_200(
    client: TestClient, admin_headers: dict, analyst_headers: dict, dataset_with_rule
) -> None:
    dataset, _, version = dataset_with_rule
    create_response = client.post(
        "/api/v1/rule-assignments", headers=admin_headers,
        json={"rule_version_id": str(version.id), "dataset_id": str(dataset.id), "assignment_scope": "DATASET_LEVEL"},
    )
    assignment_id = create_response.json()["id"]
    response = client.delete(f"/api/v1/rule-assignments/{assignment_id}", headers=analyst_headers)
    assert response.status_code == 200

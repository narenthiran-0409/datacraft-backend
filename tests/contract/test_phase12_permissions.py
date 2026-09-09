import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AIPromptVersion, Column, Connection, Dataset, Issue, RuleVersion, Schema, User
from app.modules.ai.providers import ProviderResponse
from tests.conftest import _create_user_with_role, auth_headers

RANDOM_ID = str(uuid.uuid4())

CHAT_ROUTES = [
    ("POST", "/api/v1/ai/chat", {"message": "hi"}),
    ("GET", f"/api/v1/ai/conversations/{RANDOM_ID}", None),
]
SUGGEST_ROUTES = [
    ("POST", "/api/v1/ai/suggestions/explanation", {"issue_id": RANDOM_ID}),
    ("POST", "/api/v1/ai/suggestions/run-summary", {"validation_run_id": RANDOM_ID}),
    ("POST", "/api/v1/ai/suggestions/prioritization", {"review_run_id": RANDOM_ID}),
    ("POST", "/api/v1/ai/suggestions/cluster", {"review_run_id": RANDOM_ID}),
    ("POST", "/api/v1/ai/suggestions/corrections", {"review_run_id": RANDOM_ID}),
    ("GET", f"/api/v1/ai/suggestions/{RANDOM_ID}", None),
]
ALL_ROUTES = CHAT_ROUTES + SUGGEST_ROUTES


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_phase12_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_phase12_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def publisher_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "publisher", email="ai_publisher@example.com")
    return auth_headers(user)


@pytest.fixture
def analyst_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "analyst", email="ai_analyst@example.com")
    return auth_headers(user)


@pytest.mark.parametrize("method,path,body", CHAT_ROUTES)
def test_publisher_cannot_access_chat(client: TestClient, publisher_headers: dict, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body, headers=publisher_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("method,path,body", SUGGEST_ROUTES)
def test_publisher_cannot_access_suggest(client: TestClient, publisher_headers: dict, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body, headers=publisher_headers)
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["administrator", "analyst", "approver", "reviewer"])
def test_role_can_reach_chat_endpoint(client: TestClient, db: Session, role: str, monkeypatch) -> None:
    user = _create_user_with_role(db, role, email=f"ai_chat_{role}@example.com")
    headers = auth_headers(user)
    _seed_prompt_version(db, user, "ai_chat")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider_cls("reply")}):
        response = client.post("/api/v1/ai/chat", json={"message": "hi"}, headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize("role", ["administrator", "analyst", "approver", "reviewer"])
def test_role_can_reach_suggest_endpoint(client: TestClient, db: Session, role: str) -> None:
    user = _create_user_with_role(db, role, email=f"ai_suggest_{role}@example.com")
    headers = auth_headers(user)
    response = client.get(f"/api/v1/ai/suggestions/{RANDOM_ID}", headers=headers)
    assert response.status_code == 404  # authorized, but no such suggestion — proves 403 didn't fire


def _seed_prompt_version(db: Session, user: User, prompt_key: str) -> AIPromptVersion:
    version = AIPromptVersion(
        prompt_key=prompt_key, version_number=1, template="t", default_model="claude-test",
        is_active=True, created_by=user.id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def _mock_provider_cls(text_content: str):
    fake_response = ProviderResponse(content=text_content, input_tokens=1, output_tokens=1, latency_ms=1, raw_metadata={})
    mock_cls = MagicMock()
    mock_cls.return_value.send.return_value = fake_response
    mock_cls.return_value.name = "anthropic"
    return mock_cls


# --- 200/202 with a real issue, plus 422 invalid-input matrix -----------


@pytest.fixture
def ready_issue(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.review.service import ReviewService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

    table_name = f"dq_contract12_{uuid.uuid4().hex[:8]}"
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, NULL)"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()
    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()
    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()
    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"contract12_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=val_col.id, column_ids=None, template_id=None,
    )
    validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()
    review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="c12", actor=admin_user)
    db.expire_all()
    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()

    yield issue, review_run, validation_run

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_explanation_authorized_returns_200(client: TestClient, admin_headers: dict, db: Session, admin_user: User, ready_issue, monkeypatch) -> None:
    issue, _, _ = ready_issue
    _seed_prompt_version(db, admin_user, "ai_explanation")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "fake-key")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider_cls("explanation")}):
        response = client.post("/api/v1/ai/suggestions/explanation", json={"issue_id": str(issue.id)}, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["suggestion_type"] == "EXPLANATION"


def test_run_summary_authorized_returns_202(client: TestClient, admin_headers: dict, ready_issue) -> None:
    _, _, validation_run = ready_issue
    response = client.post(
        "/api/v1/ai/suggestions/run-summary", json={"validation_run_id": str(validation_run.id)}, headers=admin_headers
    )
    assert response.status_code == 202
    assert "job_id" in response.json()


def test_run_summary_invalid_validation_run_id_returns_422(client: TestClient, admin_headers: dict) -> None:
    response = client.post("/api/v1/ai/suggestions/run-summary", json={"validation_run_id": "not-a-uuid"}, headers=admin_headers)
    assert response.status_code == 422


def test_chat_missing_message_returns_422(client: TestClient, admin_headers: dict) -> None:
    response = client.post("/api/v1/ai/chat", json={}, headers=admin_headers)
    assert response.status_code == 422


def test_response_never_discloses_anthropic_api_key(client: TestClient, admin_headers: dict, db: Session, admin_user: User, ready_issue, monkeypatch) -> None:
    issue, _, _ = ready_issue
    _seed_prompt_version(db, admin_user, "ai_explanation")
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "super-secret-value-12345")

    with patch.dict("app.modules.ai.orchestrator_service._PROVIDER_REGISTRY", {"anthropic": _mock_provider_cls("text")}):
        response = client.post("/api/v1/ai/suggestions/explanation", json={"issue_id": str(issue.id)}, headers=admin_headers)
    assert "super-secret-value-12345" not in response.text

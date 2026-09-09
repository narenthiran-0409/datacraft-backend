import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Column, Connection, Dataset, Issue, RuleVersion, Schema, User
from tests.conftest import _create_user_with_role, auth_headers

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("POST", f"/api/v1/staging-runs/{RANDOM_ID}/publish", {"target_type": "FILE_EXPORT", "target_reference": "out.jsonl"}),
    ("POST", f"/api/v1/publish-runs/{RANDOM_ID}/drift-acknowledge", {}),
    ("GET", f"/api/v1/publish-runs/{RANDOM_ID}", None),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase9_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase9_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def approver_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "approver", email="publish_approver@example.com")
    return auth_headers(user)


@pytest.fixture
def analyst_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "analyst", email="publish_analyst@example.com")
    return auth_headers(user)


@pytest.fixture
def publisher_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "publisher", email="publish_publisher@example.com")
    return auth_headers(user)


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    directory = tmp_path / "publish_exports"
    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(directory))
    return directory


@pytest.fixture
def ready_staging_run(db: Session, redis_client, admin_user: User, pg_connection: Connection, export_dir):
    """A real, successfully staged run for the 200/202/404/409 contract cases."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation
    from app.modules.approval.service import ApprovalService
    from app.modules.staging.service import StagingService
    from app.db.models import CorrectionSuggestion

    table_name = f"dq_contract9_{uuid.uuid4().hex[:8]}"
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'zzqqvalx'), (2, 'zzqqvalx'), (3, NULL)"))
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
    val_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "val")).scalar_one()

    profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(profile_job.id), str(profile_run.id))
    db.expire_all()

    rule = RulesService(db).create_rule(
        actor=admin_user, name=f"contract9_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()
    RuleAssignmentService(db).create_assignment(
        actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
        assignment_scope="SINGLE_COLUMN", column_id=val_col.id, column_ids=None, template_id=None,
    )

    validation_run, validation_job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )
    run_validation(str(validation_job.id), str(validation_run.id))
    db.expire_all()

    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name="contract9", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
    suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
    CorrectionDecisionService(db).accept(suggestion.id, admin_user)
    db.expire_all()

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user
    )
    db.expire_all()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    db.expire_all()

    yield staging_run

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_analyst_cannot_trigger_publish(client: TestClient, analyst_headers: dict, ready_staging_run) -> None:
    response = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=analyst_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out.jsonl"},
    )
    assert response.status_code == 403


def test_approver_cannot_trigger_publish(client: TestClient, approver_headers: dict, ready_staging_run) -> None:
    response = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=approver_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out.jsonl"},
    )
    assert response.status_code == 403


def test_approver_can_read_publish_run(client: TestClient, admin_headers: dict, approver_headers: dict, ready_staging_run) -> None:
    trigger = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out.jsonl"},
    )
    assert trigger.status_code == 202
    publish_run_id = trigger.json()["publish_run_id"]

    response = client.get(f"/api/v1/publish-runs/{publish_run_id}", headers=approver_headers)
    assert response.status_code == 200


def test_publisher_can_trigger_publish(client: TestClient, publisher_headers: dict, ready_staging_run) -> None:
    response = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=publisher_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out2.jsonl"},
    )
    assert response.status_code == 202
    assert "job_id" in response.json()
    assert "publish_run_id" in response.json()


def test_admin_trigger_publish_returns_202(client: TestClient, admin_headers: dict, ready_staging_run) -> None:
    response = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out3.jsonl"},
    )
    assert response.status_code == 202


def test_unsupported_target_type_returns_422(client: TestClient, admin_headers: dict, ready_staging_run) -> None:
    response = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "WAREHOUSE_TABLE", "target_reference": "out.jsonl"},
    )
    assert response.status_code == 422


def test_ineligible_staging_run_returns_409(client: TestClient, admin_headers: dict, db: Session, ready_staging_run) -> None:
    db.execute(text("UPDATE staging_runs SET status = 'BUILDING' WHERE id = :sid"), {"sid": ready_staging_run.id})
    db.commit()
    response = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out.jsonl"},
    )
    assert response.status_code == 409


def test_publish_run_not_found_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/publish-runs/{RANDOM_ID}", headers=admin_headers)
    assert response.status_code == 404


def test_second_trigger_while_in_progress_returns_409(client: TestClient, admin_headers: dict, ready_staging_run) -> None:
    first = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out4.jsonl"},
    )
    assert first.status_code == 202
    second = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out5.jsonl"},
    )
    assert second.status_code == 409


def test_analyst_cannot_acknowledge_drift(client: TestClient, admin_headers: dict, analyst_headers: dict, db: Session, ready_staging_run) -> None:
    db.execute(text("UPDATE staging_runs SET has_source_drift = true WHERE id = :sid"), {"sid": ready_staging_run.id})
    db.commit()
    trigger = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out6.jsonl"},
    )
    publish_run_id = trigger.json()["publish_run_id"]

    response = client.post(f"/api/v1/publish-runs/{publish_run_id}/drift-acknowledge", headers=analyst_headers, json={})
    assert response.status_code == 403


def test_publisher_can_acknowledge_drift(client: TestClient, admin_headers: dict, publisher_headers: dict, db: Session, ready_staging_run) -> None:
    db.execute(text("UPDATE staging_runs SET has_source_drift = true WHERE id = :sid"), {"sid": ready_staging_run.id})
    db.commit()
    trigger = client.post(
        f"/api/v1/staging-runs/{ready_staging_run.id}/publish", headers=admin_headers,
        json={"target_type": "FILE_EXPORT", "target_reference": "out7.jsonl"},
    )
    publish_run_id = trigger.json()["publish_run_id"]

    response = client.post(f"/api/v1/publish-runs/{publish_run_id}/drift-acknowledge", headers=publisher_headers, json={})
    assert response.status_code == 200
    assert response.json()["drift_acknowledged"] is True

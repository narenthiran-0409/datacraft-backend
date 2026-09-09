import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Connection, User

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("GET", "/api/v1/reviews", None),
    ("POST", "/api/v1/reviews", {"validation_run_id": RANDOM_ID}),
    ("GET", f"/api/v1/reviews/{RANDOM_ID}", None),
    ("GET", f"/api/v1/reviews/{RANDOM_ID}/issues", None),
    ("GET", f"/api/v1/reviews/{RANDOM_ID}/suggestions", None),
    ("POST", f"/api/v1/reviews/{RANDOM_ID}/generate-suggestions", {}),
    ("POST", f"/api/v1/reviews/{RANDOM_ID}/bulk-action", {"issue_ids": [], "action": "skip"}),
    ("POST", f"/api/v1/reviews/{RANDOM_ID}/archive", {}),
    ("POST", f"/api/v1/reviews/{RANDOM_ID}/restore", {}),
    ("GET", f"/api/v1/issues/{RANDOM_ID}", None),
    ("POST", f"/api/v1/suggestions/{RANDOM_ID}/accept", {}),
    ("POST", f"/api/v1/suggestions/{RANDOM_ID}/edit", {"final_value": "x"}),
    ("POST", f"/api/v1/suggestions/{RANDOM_ID}/reject", {}),
    ("POST", f"/api/v1/issues/{RANDOM_ID}/correct", {"final_value": "x"}),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase6_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase6_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def review_setup(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    """A real review run with one generated suggestion, for the 200/201/
    404/409/422 contract cases."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation
    from app.db.models import Column, Dataset, Schema

    table_name = f"dq_contract6_{uuid.uuid4().hex[:8]}"
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, 'a'), (3, NULL)"))
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
        actor=admin_user, name=f"contract6_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    from app.db.models import RuleVersion

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
        validation_run_id=validation_run.id, name="contract test", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    yield review_run

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_post_reviews_from_completed_validation_run_returns_201(client: TestClient, admin_headers: dict, db: Session, review_setup) -> None:
    validation_run_id = str(review_setup.validation_run_id)
    response = client.post("/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id})
    assert response.status_code == 201


def test_get_reviews_valid_request_returns_200(client: TestClient, admin_headers: dict, review_setup) -> None:
    response = client.get("/api/v1/reviews", headers=admin_headers)
    assert response.status_code == 200


def test_get_review_by_id_valid_request_returns_200(client: TestClient, admin_headers: dict, review_setup) -> None:
    response = client.get(f"/api/v1/reviews/{review_setup.id}", headers=admin_headers)
    assert response.status_code == 200


def test_get_review_by_id_not_found_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/reviews/{RANDOM_ID}", headers=admin_headers)
    assert response.status_code == 404


def test_get_review_issues_valid_request_returns_200(client: TestClient, admin_headers: dict, review_setup) -> None:
    response = client.get(f"/api/v1/reviews/{review_setup.id}/issues", headers=admin_headers)
    assert response.status_code == 200


def test_get_review_suggestions_valid_request_returns_200(client: TestClient, admin_headers: dict, review_setup) -> None:
    response = client.get(f"/api/v1/reviews/{review_setup.id}/suggestions", headers=admin_headers)
    assert response.status_code == 200
    assert len(response.json()) >= 1


def test_post_archive_then_archive_again_returns_409(client: TestClient, admin_headers: dict, review_setup) -> None:
    first = client.post(f"/api/v1/reviews/{review_setup.id}/archive", headers=admin_headers, json={})
    assert first.status_code == 200
    second = client.post(f"/api/v1/reviews/{review_setup.id}/archive", headers=admin_headers, json={})
    assert second.status_code == 409


def test_post_restore_after_archive_returns_200(client: TestClient, admin_headers: dict, review_setup) -> None:
    client.post(f"/api/v1/reviews/{review_setup.id}/archive", headers=admin_headers, json={})
    response = client.post(f"/api/v1/reviews/{review_setup.id}/restore", headers=admin_headers, json={})
    assert response.status_code == 200


def test_post_accept_suggestion_not_found_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.post(f"/api/v1/suggestions/{RANDOM_ID}/accept", headers=admin_headers, json={})
    assert response.status_code == 404


def test_post_edit_suggestion_blank_value_returns_422(client: TestClient, admin_headers: dict, db: Session, review_setup) -> None:
    from app.db.models import CorrectionSuggestion, Issue

    suggestion = db.execute(
        select(CorrectionSuggestion).join(Issue, Issue.id == CorrectionSuggestion.issue_id).where(Issue.review_run_id == review_setup.id)
    ).scalars().first()
    response = client.post(f"/api/v1/suggestions/{suggestion.id}/edit", headers=admin_headers, json={"final_value": "   "})
    assert response.status_code == 422


def test_post_accept_then_reject_after_accept_allowed_but_after_reject_is_409(
    client: TestClient, admin_headers: dict, db: Session, review_setup
) -> None:
    from app.db.models import CorrectionSuggestion, Issue

    suggestion = db.execute(
        select(CorrectionSuggestion).join(Issue, Issue.id == CorrectionSuggestion.issue_id).where(Issue.review_run_id == review_setup.id)
    ).scalars().first()

    reject_response = client.post(f"/api/v1/suggestions/{suggestion.id}/reject", headers=admin_headers, json={})
    assert reject_response.status_code == 200

    accept_after_reject = client.post(f"/api/v1/suggestions/{suggestion.id}/accept", headers=admin_headers, json={})
    assert accept_after_reject.status_code == 409

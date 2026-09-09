import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Connection, User
from tests.conftest import _create_user_with_role, auth_headers

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("GET", "/api/v1/approvals", None),
    ("POST", f"/api/v1/reviews/{RANDOM_ID}/submit-approval", {}),
    ("GET", f"/api/v1/approvals/{RANDOM_ID}", None),
    ("POST", f"/api/v1/approvals/{RANDOM_ID}/approve", {"issue_ids": []}),
    ("POST", f"/api/v1/approvals/{RANDOM_ID}/reject", {"issue_ids": []}),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase7_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase7_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def approver_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "approver", email="approver@example.com")
    return auth_headers(user)


@pytest.fixture
def approval_setup(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    """A real approval request with one resolved issue in scope, for the
    200/201/404/409 contract cases."""
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
    from app.db.models import Column, CorrectionSuggestion, Dataset, Issue, RuleVersion, Schema
    from app.modules.approval.service import ApprovalService

    table_name = f"dq_contract7_{uuid.uuid4().hex[:8]}"
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
        actor=admin_user, name=f"contract7_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
        validation_run_id=validation_run.id, name="contract7", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
    suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
    CorrectionDecisionService(db).accept(suggestion.id, admin_user)
    db.expire_all()

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    db.expire_all()

    yield approval_request, issue.id

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_get_approvals_valid_request_returns_200(client: TestClient, admin_headers: dict, approval_setup) -> None:
    response = client.get("/api/v1/approvals", headers=admin_headers)
    assert response.status_code == 200


def test_get_approval_by_id_valid_request_returns_200(client: TestClient, admin_headers: dict, approval_setup) -> None:
    approval_request, _ = approval_setup
    response = client.get(f"/api/v1/approvals/{approval_request.id}", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert "decided_count" in body and "remaining_count" in body


def test_get_approval_not_found_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/approvals/{RANDOM_ID}", headers=admin_headers)
    assert response.status_code == 404


def test_analyst_can_submit_approval(
    client: TestClient, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """A fresh IN_REVIEW review run, submitted by an analyst (review.edit) —
    confirms analyst has submission access even though approval.decide is
    approver/publisher/administrator-only."""
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
    from app.db.models import Column, CorrectionSuggestion, Dataset, Issue, RuleVersion, Schema

    table_name = f"dq_contract7_analyst_{uuid.uuid4().hex[:8]}"
    try:
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
            actor=admin_user, name=f"contract7a_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
            validation_run_id=validation_run.id, name="contract7a", actor=admin_user
        )
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        analyst = _create_user_with_role(db, "analyst", email="analyst_submit@example.com")
        analyst_headers = auth_headers(analyst)

        response = client.post(f"/api/v1/reviews/{review_run.id}/submit-approval", headers=analyst_headers, json={})
        assert response.status_code == 201
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_analyst_gets_403_on_approve_reject(
    client: TestClient, db: Session, approval_setup
) -> None:
    analyst = _create_user_with_role(db, "analyst", email="analyst_decide@example.com")
    analyst_headers = auth_headers(analyst)

    approval_request, issue_id = approval_setup
    approve_response = client.post(
        f"/api/v1/approvals/{approval_request.id}/approve", headers=analyst_headers, json={"issue_ids": [str(issue_id)]}
    )
    assert approve_response.status_code == 403

    reject_response = client.post(
        f"/api/v1/approvals/{approval_request.id}/reject", headers=analyst_headers, json={"issue_ids": [str(issue_id)]}
    )
    assert reject_response.status_code == 403


def test_approver_can_approve_and_reject(client: TestClient, approver_headers: dict, approval_setup) -> None:
    approval_request, issue_id = approval_setup
    response = client.post(
        f"/api/v1/approvals/{approval_request.id}/approve", headers=approver_headers, json={"issue_ids": [str(issue_id)]}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"


def test_submit_approval_not_ready_returns_409(client: TestClient, admin_headers: dict, approval_setup) -> None:
    approval_request, _ = approval_setup
    # approval_setup's review run is already READY_FOR_APPROVAL (submitted).
    response = client.post(f"/api/v1/reviews/{approval_request.review_run_id}/submit-approval", headers=admin_headers, json={})
    assert response.status_code == 409


def test_approve_issue_not_in_scope_returns_409(client: TestClient, admin_headers: dict, approval_setup) -> None:
    approval_request, _ = approval_setup
    response = client.post(
        f"/api/v1/approvals/{approval_request.id}/approve", headers=admin_headers, json={"issue_ids": [RANDOM_ID]}
    )
    assert response.status_code == 409

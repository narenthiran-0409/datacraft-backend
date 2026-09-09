import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Connection, User
from tests.conftest import _create_user_with_role, auth_headers

RANDOM_ID = str(uuid.uuid4())

PERMISSION_ROUTES = [
    ("POST", f"/api/v1/reviews/{RANDOM_ID}/staging", None),
    ("GET", f"/api/v1/staging-runs/{RANDOM_ID}", None),
    ("GET", f"/api/v1/staging-runs/{RANDOM_ID}/records", None),
]


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase8_route_rejects_missing_token(client: TestClient, method: str, path: str, body) -> None:
    response = client.request(method, path, json=body)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", PERMISSION_ROUTES)
def test_phase8_route_rejects_token_without_permission(
    client: TestClient, no_role_headers: dict, method: str, path: str, body
) -> None:
    response = client.request(method, path, json=body, headers=no_role_headers)
    assert response.status_code == 403


@pytest.fixture
def approver_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "approver", email="staging_approver@example.com")
    return auth_headers(user)


@pytest.fixture
def analyst_headers(db: Session) -> dict:
    user = _create_user_with_role(db, "analyst", email="staging_analyst@example.com")
    return auth_headers(user)


@pytest.fixture
def staged_run(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    """A real, successfully staged run for the 200/201/404 contract cases."""
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
    from app.db.models import Column, CorrectionSuggestion, Dataset, Issue, RuleVersion, Schema

    table_name = f"dq_contract8_{uuid.uuid4().hex[:8]}"
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
        actor=admin_user, name=f"contract8_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
        validation_run_id=validation_run.id, name="contract8", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
    suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
    CorrectionDecisionService(db).accept(suggestion.id, admin_user)
    db.expire_all()

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    approval_request = ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user
    )
    db.expire_all()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    db.expire_all()

    yield review_run, staging_run

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


def test_get_staging_run_valid_request_returns_200(client: TestClient, admin_headers: dict, staged_run) -> None:
    _, staging_run = staged_run
    response = client.get(f"/api/v1/staging-runs/{staging_run.id}", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "READY"


def test_get_staging_run_not_found_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/staging-runs/{RANDOM_ID}", headers=admin_headers)
    assert response.status_code == 404


def test_get_staging_records_valid_request_returns_200(client: TestClient, admin_headers: dict, staged_run) -> None:
    _, staging_run = staged_run
    response = client.get(f"/api/v1/staging-runs/{staging_run.id}/records", headers=admin_headers)
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_get_staging_records_not_found_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/staging-runs/{RANDOM_ID}/records", headers=admin_headers)
    assert response.status_code == 404


def test_analyst_cannot_trigger_staging(client: TestClient, analyst_headers: dict, staged_run) -> None:
    review_run, _ = staged_run
    response = client.post(f"/api/v1/reviews/{review_run.id}/staging", headers=analyst_headers, json={})
    assert response.status_code == 403


def test_analyst_can_read_staging(client: TestClient, analyst_headers: dict, staged_run) -> None:
    _, staging_run = staged_run
    response = client.get(f"/api/v1/staging-runs/{staging_run.id}", headers=analyst_headers)
    assert response.status_code == 200


def test_approver_can_trigger_staging(client: TestClient, approver_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection) -> None:
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
    from app.db.models import Column, CorrectionSuggestion, Dataset, Issue, RuleVersion, Schema

    table_name = f"dq_contract8_approver_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'a'), (2, 'a'), (3, NULL)"))
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
            actor=admin_user, name=f"contract8b_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="contract8b", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()
        approval_request = ApprovalService(db).submit(review_run.id, admin_user)
        ApprovalService(db).decide(approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user)
        db.expire_all()

        response = client.post(f"/api/v1/reviews/{review_run.id}/staging", headers=approver_headers, json={})
        assert response.status_code == 201
        assert response.json()["status"] == "READY"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_trigger_staging_no_approval_returns_409(client: TestClient, admin_headers: dict, db: Session, staged_run) -> None:
    # Reuse staged_run's review run, but reset its status back to IN_REVIEW
    # so this checks the "no APPROVED request" precondition specifically,
    # not the "not IN_REVIEW" one (the fixture's run is READY_FOR_APPROVAL
    # after staging succeeded once already).
    review_run, _ = staged_run
    db.execute(text("UPDATE review_runs SET status = 'IN_REVIEW' WHERE id = :rid"), {"rid": review_run.id})
    db.execute(text("UPDATE approval_requests SET status = 'REJECTED' WHERE review_run_id = :rid"), {"rid": review_run.id})
    db.commit()

    response = client.post(f"/api/v1/reviews/{review_run.id}/staging", headers=admin_headers, json={})
    assert response.status_code == 409

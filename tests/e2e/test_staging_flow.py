"""End-to-end: validation -> review -> correction -> approval -> APPROVED
-> staging -> verify staging_records content. Also explicitly verifies a
rejected approval never produces staging_records, and that corrections/
approval tables are unmodified by the whole flow. Mirrors
tests/e2e/test_approval_flow.py's structure.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import ApprovalDecision, ApprovalDecisionIssue, ApprovalRequest, Column, Connection, Correction, Dataset, Schema, User


def test_full_staging_flow(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_staging_{uuid.uuid4().hex[:8]}"
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, category TEXT)"))
            db.execute(
                text(
                    f"INSERT INTO {table_name} VALUES (1, 'A'), (2, 'A'), (3, NULL), (4, 'B')"
                )
            )
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

            profile_response = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})
            assert profile_response.status_code == 202

            category_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "category")).scalar_one()

            rule_response = client.post(
                "/api/v1/rules", headers=admin_headers,
                json={"name": f"e2e_staging_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
            )
            assert rule_response.status_code == 201
            version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
            assignment_response = client.post(
                "/api/v1/rule-assignments", headers=admin_headers,
                json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(category_col.id)},
            )
            assert assignment_response.status_code == 201

            validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
            assert validate_response.status_code == 202
            validation_run_id = validate_response.json()["id"]

            review_response = client.post(
                "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e staging"}
            )
            assert review_response.status_code == 201
            review_id = review_response.json()["id"]

            generate_response = client.post(f"/api/v1/reviews/{review_id}/generate-suggestions", headers=admin_headers, json={})
            assert generate_response.status_code == 200

            suggestions = client.get(f"/api/v1/reviews/{review_id}/suggestions", headers=admin_headers).json()
            assert len(suggestions) == 1
            suggestion = suggestions[0]
            assert suggestion["suggested_value"] == "A"  # mode of ['A','A','B']

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
            assert approve_response.json()["status"] == "APPROVED"

            corrections_before = {
                str(c.issue_id): (c.final_value, c.status)
                for c in db.execute(select(Correction).where(Correction.issue_id == issue_id)).scalars()
            }
            approval_rows_before = (
                len(db.execute(select(ApprovalRequest)).scalars().all()),
                len(db.execute(select(ApprovalDecision)).scalars().all()),
                len(db.execute(select(ApprovalDecisionIssue)).scalars().all()),
            )

            staging_response = client.post(f"/api/v1/reviews/{review_id}/staging", headers=admin_headers, json={})
            assert staging_response.status_code == 201
            staging_run_id = staging_response.json()["id"]
            assert staging_response.json()["status"] == "READY"
            assert staging_response.json()["record_count"] == 1
            assert staging_response.json()["field_count"] == 1

            get_response = client.get(f"/api/v1/staging-runs/{staging_run_id}", headers=admin_headers)
            assert get_response.status_code == 200
            assert get_response.json()["has_source_drift"] is False

            records_response = client.get(f"/api/v1/staging-runs/{staging_run_id}/records", headers=admin_headers)
            assert records_response.status_code == 200
            records = records_response.json()
            assert len(records) == 1
            record = records[0]
            assert record["source_drift_status"] == "UNCHANGED"
            assert record["row_snapshot"]["category"] == "A"  # the approved correction
            assert record["row_snapshot"]["id"] == 3  # unchanged, unrelated field preserved verbatim
            assert record["corrected_fields"][0]["column_name"] == "category"
            assert record["corrected_fields"][0]["final_value"] == "A"

            # Nothing in Phase 6/7 was ever modified by staging.
            corrections_after = {
                str(c.issue_id): (c.final_value, c.status)
                for c in db.execute(select(Correction).where(Correction.issue_id == issue_id)).scalars()
            }
            assert corrections_before == corrections_after
            approval_rows_after = (
                len(db.execute(select(ApprovalRequest)).scalars().all()),
                len(db.execute(select(ApprovalDecision)).scalars().all()),
                len(db.execute(select(ApprovalDecisionIssue)).scalars().all()),
            )
            assert approval_rows_before == approval_rows_after
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_rejected_approval_never_produces_staging_records(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_staging_rejected_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, category TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, 'A'), (2, 'A'), (3, NULL)"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

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
        from app.core.exceptions import ApprovalNotApprovedError
        from app.db.models import CorrectionSuggestion, Issue, RuleVersion, StagingRun
        import pytest

        job = JobsService(db, redis_client).create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()
        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "category")).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"e2e_rejected_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = db.execute(select(RuleVersion).where(RuleVersion.rule_id == rule.id)).scalar_one()
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
        )
        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()
        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="e2e_rejected", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()
        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        approval_request = ApprovalService(db).submit(review_run.id, admin_user)
        approval_request = ApprovalService(db).decide(
            approval_request.id, decision="REJECT", issue_ids=[issue.id], comment=None, actor=admin_user
        )
        assert approval_request.status == "REJECTED"
        db.expire_all()

        with pytest.raises(ApprovalNotApprovedError):
            StagingService(db).trigger(review_run.id, admin_user)

        staging_runs = db.execute(select(StagingRun).where(StagingRun.review_run_id == review_run.id)).scalars().all()
        assert staging_runs == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

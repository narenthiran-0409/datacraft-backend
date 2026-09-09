"""End-to-end: real validation -> create a review from it -> generate
suggestions -> accept some, edit some, reject some, skip some, direct-
correct an issue with zero suggestions -> confirm the review run's issue
list reflects every terminal state correctly -> archive -> restore.
Mirrors tests/e2e/test_validation_flow.py's structure.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.db.models import Column, Connection, Dataset, Schema, User


def _make_dataset_with_profile(client: TestClient, admin_headers, db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str) -> Dataset:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, category TEXT, note TEXT, score NUMERIC)"))
    db.execute(
        text(
            f"INSERT INTO {table_name} VALUES "
            "(1, 'A', 'AB-123', 10), (2, 'A', 'AB-124', 20), (3, NULL, '  AB-125  ', 500), "
            "(4, 'B', 'zzz', 999)"
        )
    )
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()

    profile_response = client.post(f"/api/v1/datasets/{dataset.id}/profile", headers=admin_headers, json={"full_scan": True})
    assert profile_response.status_code == 202

    return dataset


def test_full_review_flow(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name = f"dq_e2e_review_{uuid.uuid4().hex[:8]}"
    try:
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        try:
            dataset = _make_dataset_with_profile(client, admin_headers, db, redis_client, admin_user, pg_connection, table_name)

            category_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "category")).scalar_one()
            note_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "note")).scalar_one()
            score_col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "score")).scalar_one()

            rule_response = client.post(
                "/api/v1/rules", headers=admin_headers,
                json={"name": f"e2e_completeness_{uuid.uuid4().hex[:8]}", "rule_type": "COMPLETENESS", "definition": {"max_null_percentage": 0}},
            )
            assert rule_response.status_code == 201
            version_id = client.get(f"/api/v1/rules/{rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
            assignment_response = client.post(
                "/api/v1/rule-assignments", headers=admin_headers,
                json={"rule_version_id": version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(category_col.id)},
            )
            assert assignment_response.status_code == 201

            pattern_rule_response = client.post(
                "/api/v1/rules", headers=admin_headers,
                json={"name": f"e2e_pattern_{uuid.uuid4().hex[:8]}", "rule_type": "PATTERN", "definition": {"regex": r"^[A-Z]{2}-\d{3}$"}},
            )
            assert pattern_rule_response.status_code == 201
            pattern_version_id = client.get(f"/api/v1/rules/{pattern_rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
            client.post(
                "/api/v1/rule-assignments", headers=admin_headers,
                json={"rule_version_id": pattern_version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(note_col.id)},
            )

            range_rule_response = client.post(
                "/api/v1/rules", headers=admin_headers,
                json={"name": f"e2e_range_{uuid.uuid4().hex[:8]}", "rule_type": "RANGE", "definition": {"min": 0, "max": 100}},
            )
            assert range_rule_response.status_code == 201
            range_version_id = client.get(f"/api/v1/rules/{range_rule_response.json()['id']}/versions", headers=admin_headers).json()[0]["id"]
            client.post(
                "/api/v1/rule-assignments", headers=admin_headers,
                json={"rule_version_id": range_version_id, "dataset_id": str(dataset.id), "assignment_scope": "SINGLE_COLUMN", "column_id": str(score_col.id)},
            )

            validate_response = client.post(f"/api/v1/datasets/{dataset.id}/validate", headers=admin_headers, json={})
            assert validate_response.status_code == 202
            validation_run_id = validate_response.json()["id"]

            review_response = client.post(
                "/api/v1/reviews", headers=admin_headers, json={"validation_run_id": validation_run_id, "name": "e2e review"}
            )
            assert review_response.status_code == 201
            review_id = review_response.json()["id"]
            assert review_response.json()["status"] == "DRAFT"

            issues_response = client.get(f"/api/v1/reviews/{review_id}/issues", headers=admin_headers)
            assert issues_response.status_code == 200
            all_issues = issues_response.json()
            assert len(all_issues) == 5  # category NULL, note whitespace, note bad-format, 2x score out-of-range

            generate_response = client.post(f"/api/v1/reviews/{review_id}/generate-suggestions", headers=admin_headers, json={})
            assert generate_response.status_code == 200
            assert generate_response.json()["generated_count"] == 4  # mode_fill + trim_whitespace + 2x range_clamp
            assert generate_response.json()["issues_with_no_suggestion_count"] == 1  # 'zzz' PATTERN failure, unfixable by trim

            suggestions_response = client.get(f"/api/v1/reviews/{review_id}/suggestions", headers=admin_headers)
            assert suggestions_response.status_code == 200
            suggestions = suggestions_response.json()
            assert len(suggestions) == 4

            mode_suggestion = next(s for s in suggestions if s["fix_type"] == "mode_fill")
            trim_suggestion = next(s for s in suggestions if s["fix_type"] == "trim_whitespace")
            range_suggestions = [s for s in suggestions if s["fix_type"] == "range_clamp"]
            assert len(range_suggestions) == 2

            # Accept the mode_fill suggestion.
            accept_response = client.post(f"/api/v1/suggestions/{mode_suggestion['id']}/accept", headers=admin_headers, json={})
            assert accept_response.status_code == 200
            assert accept_response.json()["status"] == "ACCEPTED"

            # Edit the trim_whitespace suggestion instead of accepting it.
            edit_response = client.post(
                f"/api/v1/suggestions/{trim_suggestion['id']}/edit", headers=admin_headers, json={"final_value": "AB-999"}
            )
            assert edit_response.status_code == 200
            assert edit_response.json()["status"] == "EDITED"
            assert edit_response.json()["value_source"] == "HUMAN"

            # Reject one range_clamp suggestion, skip the issue behind the other.
            reject_response = client.post(f"/api/v1/suggestions/{range_suggestions[0]['id']}/reject", headers=admin_headers, json={"reason": "not applicable"})
            assert reject_response.status_code == 200
            assert reject_response.json()["status"] == "REJECTED"

            skip_issue_id = range_suggestions[1]["issue_id"]
            skip_response = client.post(
                f"/api/v1/reviews/{review_id}/bulk-action", headers=admin_headers,
                json={"issue_ids": [skip_issue_id], "action": "skip"},
            )
            assert skip_response.status_code == 200
            assert skip_response.json()["issue_count"] == 1

            # Direct-correct the zero-suggestion issue ('zzz').
            no_suggestion_issue = next(i for i in all_issues if i["column_id"] == str(note_col.id) and i["original_value"] == "zzz")
            correct_response = client.post(
                f"/api/v1/issues/{no_suggestion_issue['id']}/correct", headers=admin_headers, json={"final_value": "AB-000"}
            )
            assert correct_response.status_code == 200
            assert correct_response.json()["status"] == "EDITED"
            assert correct_response.json()["correction_suggestion_id"] is None

            final_issues_response = client.get(f"/api/v1/reviews/{review_id}/issues", headers=admin_headers)
            final_issues = final_issues_response.json()
            statuses = {i["id"]: i["status"] for i in final_issues}
            assert statuses[skip_issue_id] == "SKIPPED"
            for issue in final_issues:
                if issue["id"] != skip_issue_id:
                    assert issue["status"] == "RESOLVED"

            archive_response = client.post(f"/api/v1/reviews/{review_id}/archive", headers=admin_headers, json={})
            assert archive_response.status_code == 200
            assert archive_response.json()["status"] == "ARCHIVED"

            restore_response = client.post(f"/api/v1/reviews/{review_id}/restore", headers=admin_headers, json={})
            assert restore_response.status_code == 200
            assert restore_response.json()["status"] == "IN_REVIEW"
        finally:
            celery_app.conf.task_always_eager = False
            celery_app.conf.task_eager_propagates = False
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

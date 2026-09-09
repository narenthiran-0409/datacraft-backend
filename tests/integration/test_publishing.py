"""Integration tests for Phase 9 (Publishing) against a real local Postgres
instance + real Celery task execution (called directly, synchronously —
task_always_eager is used only in the e2e test), mirroring
tests/integration/test_staging.py's structure.
"""
import json
import threading
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.exceptions import (
    NoDriftToAcknowledgeError,
    PublishAlreadyInProgressError,
    PublishRunNotFoundError,
    StagingRunNotEligibleError,
    TargetTypeNotSupportedError,
)
from app.db.models import (
    ApprovalDecision,
    ApprovalDecisionIssue,
    ApprovalRequest,
    AuditEvent,
    Column,
    Connection,
    Correction,
    CorrectionSuggestion,
    Dataset,
    Issue,
    Job,
    PublishRun,
    ReviewRun,
    Schema,
    StagingRecord,
    StagingRun,
    User,
)
from app.modules.approval.service import ApprovalService
from app.modules.jobs.service import JobsService
from app.modules.publishing.service import PublishingService
from app.modules.publishing.tasks import run_publish
from app.modules.review.decision_service import CorrectionDecisionService
from app.modules.review.service import ReviewService
from app.modules.review.suggestion_service import SuggestionService
from app.modules.rules.service import RuleAssignmentService, RulesService
from app.modules.staging.service import StagingService
from app.modules.validation.service import ValidationService
from app.modules.validation.tasks import run_validation


def _build_ready_staging_run(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str):
    """Builds a table with 1 column, 1 row needing correction, and runs it
    all the way through discover -> profile -> validate -> review -> correct
    -> approve -> stage. Returns the resulting (staging_run, review_run,
    dataset, table_name)."""
    from app.modules.discovery.tasks import run_discovery
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile

    # A distinctive multi-char value (not a common substring like 'a') so
    # test_audit_events_contain_no_raw_values' negative-containment check
    # against audit metadata (UUIDs, enum names) isn't a false positive.
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
        actor=admin_user, name=f"pub_{uuid.uuid4().hex[:8]}", description=None, category=None,
        rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
        error_message_template=None,
    )
    version = RulesService(db).list_versions(rule.id)[0]
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
        validation_run_id=validation_run.id, name=f"pub_{uuid.uuid4().hex[:6]}", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
    suggestion = db.execute(
        select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)
    ).scalars().first()
    CorrectionDecisionService(db).accept(suggestion.id, admin_user)
    db.expire_all()

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user
    )
    db.expire_all()

    staging_run = StagingService(db).trigger(review_run.id, admin_user)
    db.expire_all()

    return db.get(StagingRun, staging_run.id), db.get(ReviewRun, review_run.id), dataset, table_name


@pytest.fixture
def ready_staging_run(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    table_name = f"dq_publish_{uuid.uuid4().hex[:8]}"
    try:
        staging_run, review_run, dataset, _ = _build_ready_staging_run(
            db, redis_client, admin_user, pg_connection, table_name
        )
        assert staging_run.status == "READY"
        yield staging_run, review_run, dataset, table_name
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


@pytest.fixture
def export_dir(tmp_path, monkeypatch):
    directory = tmp_path / "publish_exports"
    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(directory))
    return directory


def test_non_ready_staging_run_returns_409_no_row_created(db: Session, admin_user: User) -> None:
    staging_run = db.execute(select(StagingRun)).scalars().first()
    if staging_run is None:
        pytest.skip("no staging_run available")
    db.execute(text("UPDATE staging_runs SET status = 'BUILDING' WHERE id = :sid"), {"sid": staging_run.id})
    db.commit()
    db.expire_all()

    before = db.execute(select(PublishRun)).scalars().all()
    with pytest.raises(StagingRunNotEligibleError):
        PublishingService(db).trigger(
            staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
        )
    after = db.execute(select(PublishRun)).scalars().all()
    assert len(before) == len(after)


def test_non_current_staging_run_returns_409(db: Session, admin_user: User, ready_staging_run) -> None:
    staging_run, review_run, _, _ = ready_staging_run
    db.execute(text("UPDATE staging_runs SET is_current = false WHERE id = :sid"), {"sid": staging_run.id})
    db.commit()
    db.expire_all()

    with pytest.raises(StagingRunNotEligibleError):
        PublishingService(db).trigger(
            staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
        )


@pytest.mark.parametrize("target_type", ["WAREHOUSE_TABLE", "API", "SOURCE_TABLE"])
def test_unsupported_target_type_returns_422_no_row_created(
    db: Session, admin_user: User, ready_staging_run, target_type: str
) -> None:
    staging_run, _, _, _ = ready_staging_run
    before = db.execute(select(PublishRun)).scalars().all()
    with pytest.raises(TargetTypeNotSupportedError):
        PublishingService(db).trigger(
            staging_run.id, target_type=target_type, target_reference="out.jsonl", overwrite=False, actor=admin_user
        )
    after = db.execute(select(PublishRun)).scalars().all()
    assert len(before) == len(after)


def test_publish_run_not_found_returns_404(db: Session) -> None:
    with pytest.raises(PublishRunNotFoundError):
        PublishingService(db).get(uuid.uuid4())


def test_successful_file_export_publishes_and_verifies_content(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    assert publish_run.status == "PENDING"

    result = run_publish(str(job.id), str(publish_run.id))
    assert result["status"] == "PUBLISHED"

    db.expire_all()
    refreshed = db.get(PublishRun, publish_run.id)
    assert refreshed.status == "PUBLISHED"
    assert refreshed.published_record_count == 1

    output_path = export_dir / "out.jsonl"
    assert output_path.exists()
    lines = output_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["val"] == "zzqqvalx"  # the approved correction, from row_snapshot
    assert row["id"] == 3


def test_path_traversal_rejected_no_file_written(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="../escape.jsonl", overwrite=False, actor=admin_user
    )
    result = run_publish(str(job.id), str(publish_run.id))
    assert result["status"] == "FAILED"

    db.expire_all()
    refreshed = db.get(PublishRun, publish_run.id)
    assert refreshed.status == "FAILED"
    assert "InvalidTargetPathError" in refreshed.error_message
    assert not (export_dir.parent / "escape.jsonl").exists()


def test_no_silent_overwrite_rejected_file_untouched(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / "existing.jsonl"
    target.write_text("PRE_EXISTING_CONTENT")

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="existing.jsonl", overwrite=False, actor=admin_user
    )
    result = run_publish(str(job.id), str(publish_run.id))
    assert result["status"] == "FAILED"

    db.expire_all()
    assert db.get(PublishRun, publish_run.id).status == "FAILED"
    assert target.read_text(encoding="utf-8") == "PRE_EXISTING_CONTENT"


def test_overwrite_flag_permits_replacing_existing_file(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / "existing.jsonl"
    target.write_text("PRE_EXISTING_CONTENT")

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="existing.jsonl", overwrite=True, actor=admin_user
    )
    result = run_publish(str(job.id), str(publish_run.id))
    assert result["status"] == "PUBLISHED"
    assert "PRE_EXISTING_CONTENT" not in target.read_text(encoding="utf-8")


def test_retry_after_failed_creates_independent_new_row(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run

    first, first_job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="../escape.jsonl", overwrite=False, actor=admin_user
    )
    run_publish(str(first_job.id), str(first.id))
    db.expire_all()
    assert db.get(PublishRun, first.id).status == "FAILED"

    second, second_job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    assert second.id != first.id
    assert second_job.id != first_job.id
    result = run_publish(str(second_job.id), str(second.id))
    assert result["status"] == "PUBLISHED"

    all_runs = db.execute(select(PublishRun).where(PublishRun.staging_run_id == staging_run.id)).scalars().all()
    assert len(all_runs) == 2


def test_concurrent_trigger_calls_exactly_one_succeeds(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run
    db.commit()

    staging_run_id = staging_run.id
    admin_user_id = admin_user.id
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _worker(name: str) -> None:
        thread_db = SessionLocal()
        try:
            thread_user = thread_db.get(User, admin_user_id)
            service = PublishingService(thread_db)
            barrier.wait(timeout=10)
            try:
                publish_run, _ = service.trigger(
                    staging_run_id, target_type="FILE_EXPORT", target_reference=f"{name}.jsonl",
                    overwrite=False, actor=thread_user,
                )
                results[name] = ("success", publish_run.status)
            except PublishAlreadyInProgressError as exc:
                results[name] = ("blocked", str(exc))
        finally:
            thread_db.close()

    t1 = threading.Thread(target=_worker, args=("t1",))
    t2 = threading.Thread(target=_worker, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert "t1" in results and "t2" in results
    outcomes = [results["t1"], results["t2"]]
    assert len([o for o in outcomes if o[0] == "success"]) == 1
    assert len([o for o in outcomes if o[0] == "blocked"]) == 1

    db.expire_all()
    all_runs = db.execute(select(PublishRun).where(PublishRun.staging_run_id == staging_run_id)).scalars().all()
    assert len(all_runs) == 1


def test_publishing_never_writes_frozen_tables(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, review_run, _, _ = ready_staging_run

    corrections_before = list(db.execute(select(Correction)).scalars())
    issues_before = list(db.execute(select(Issue)).scalars())
    approval_requests_before = list(db.execute(select(ApprovalRequest)).scalars())
    approval_decisions_before = list(db.execute(select(ApprovalDecision)).scalars())
    approval_decision_issues_before = list(db.execute(select(ApprovalDecisionIssue)).scalars())
    staging_runs_before = {r.id: (r.status, r.is_current, r.record_count) for r in db.execute(select(StagingRun)).scalars()}
    staging_records_before = {r.id: (r.row_snapshot, r.corrected_fields) for r in db.execute(select(StagingRecord)).scalars()}

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    run_publish(str(job.id), str(publish_run.id))
    db.expire_all()

    assert [(c.id, c.final_value, c.status) for c in corrections_before] == [
        (c.id, c.final_value, c.status) for c in db.execute(select(Correction)).scalars()
    ]
    assert len(issues_before) == len(db.execute(select(Issue)).scalars().all())
    assert len(approval_requests_before) == len(db.execute(select(ApprovalRequest)).scalars().all())
    assert len(approval_decisions_before) == len(db.execute(select(ApprovalDecision)).scalars().all())
    assert len(approval_decision_issues_before) == len(db.execute(select(ApprovalDecisionIssue)).scalars().all())

    staging_runs_after = {r.id: (r.status, r.is_current, r.record_count) for r in db.execute(select(StagingRun)).scalars()}
    assert staging_runs_before == staging_runs_after
    staging_records_after = {r.id: (r.row_snapshot, r.corrected_fields) for r in db.execute(select(StagingRecord)).scalars()}
    assert staging_records_before == staging_records_after


def test_audit_events_contain_no_raw_values(db: Session, admin_user: User, ready_staging_run, export_dir) -> None:
    staging_run, _, _, _ = ready_staging_run
    corrections = list(db.execute(select(Correction)).scalars())
    final_values = [c.final_value for c in corrections if c.final_value]

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    run_publish(str(job.id), str(publish_run.id))

    events = db.execute(
        select(AuditEvent).where(
            AuditEvent.action.in_(("publish_run.created", "publish_run.completed", "publish_run.failed"))
        )
    ).scalars().all()
    assert len(events) >= 2
    for event in events:
        blob = str(event.before_value) + str(event.after_value) + str(event.audit_metadata)
        for value in final_values:
            assert value not in blob


# --- Drift-acknowledge re-enqueue lifecycle (approved resolution) ---


def _force_drift(db: Session, staging_run_id) -> None:
    db.execute(text("UPDATE staging_runs SET has_source_drift = true WHERE id = :sid"), {"sid": staging_run_id})
    db.commit()


def test_drifted_staging_run_creates_pending_publish_attempt(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run
    _force_drift(db, staging_run.id)

    publish_run, _ = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    assert publish_run.status == "PENDING"


def test_initial_task_execution_does_not_publish_while_drift_unacknowledged(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run
    _force_drift(db, staging_run.id)

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    result = run_publish(str(job.id), str(publish_run.id))
    assert result["status"] == "PENDING"
    assert result["reason"] == "drift_not_acknowledged"

    db.expire_all()
    refreshed = db.get(PublishRun, publish_run.id)
    assert refreshed.status == "PENDING"
    assert not (export_dir / "out.jsonl").exists()


def test_drift_acknowledge_sets_three_fields_reuses_row_and_publishes(
    db: Session, admin_user: User, ready_staging_run, export_dir
) -> None:
    staging_run, _, _, _ = ready_staging_run
    _force_drift(db, staging_run.id)

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    run_publish(str(job.id), str(publish_run.id))  # first dequeue: blocked, stays PENDING
    db.expire_all()

    all_before = db.execute(select(PublishRun)).scalars().all()
    jobs_before = db.execute(select(Job)).scalars().all()

    acknowledged = PublishingService(db).acknowledge_drift(publish_run.id, comment="reviewed and accepted", actor=admin_user)
    assert acknowledged.drift_acknowledged is True
    assert acknowledged.drift_acknowledged_by == admin_user.id
    assert acknowledged.drift_acknowledged_at is not None
    assert acknowledged.id == publish_run.id  # #5 same publish_run_id
    assert acknowledged.job_id == job.id  # #5 same job_id

    # PublishingService.acknowledge_drift() itself never dispatches Celery
    # (that's the API route's job, mirroring ProfilingService/ValidationService
    # convention) — call run_publish directly here to simulate exactly the
    # re-enqueued delivery the route's run_publish.delay(...) would trigger.
    result = run_publish(str(job.id), str(publish_run.id))
    assert result["status"] == "PUBLISHED"  # #6, #7

    db.expire_all()
    final = db.get(PublishRun, publish_run.id)
    assert final.status == "PUBLISHED"

    all_after = db.execute(select(PublishRun)).scalars().all()
    jobs_after = db.execute(select(Job)).scalars().all()
    assert len(all_before) == len(all_after)  # #8 no second publish_runs row
    assert len(jobs_before) == len(jobs_after)  # #8 no second jobs row

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == publish_run.id)
    ).scalars().all()
    actions = {e.action for e in events}
    assert "publish_run.drift_acknowledged" in actions  # #9
    assert "publish_run.completed" in actions  # #9


def test_acknowledge_drift_without_drift_raises(db: Session, admin_user: User, ready_staging_run, export_dir) -> None:
    staging_run, _, _, _ = ready_staging_run
    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    with pytest.raises(NoDriftToAcknowledgeError):
        PublishingService(db).acknowledge_drift(publish_run.id, comment=None, actor=admin_user)


def test_duplicate_task_delivery_is_idempotent(db: Session, admin_user: User, ready_staging_run, export_dir) -> None:
    staging_run, _, _, _ = ready_staging_run
    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="out.jsonl", overwrite=False, actor=admin_user
    )
    first = run_publish(str(job.id), str(publish_run.id))
    assert first["status"] == "PUBLISHED"

    # Simulated redelivery of the exact same Celery message after PUBLISHED.
    second = run_publish(str(job.id), str(publish_run.id))
    assert second["status"] == "COMPLETED"  # job.status, per the idempotency-guard early return

    db.expire_all()
    refreshed = db.get(PublishRun, publish_run.id)
    assert refreshed.status == "PUBLISHED"
    assert refreshed.published_record_count == 1  # unchanged by the redelivery

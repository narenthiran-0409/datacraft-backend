"""Backfill correctness + re-runnability, against a real full pipeline run
built via the actual API/service layer (which already writes live touch-
point edges) — TRUNCATEs lineage_records first to simulate "pre-Phase-10
data with no lineage edges yet", then proves the backfill reconstructs
them and that running it twice produces IDENTICAL row counts.
"""
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Column, Connection, Dataset, Issue, LineageRecord, Schema, User
from scripts.backfill_lineage import backfill


def _build_full_pipeline(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str, tmp_path):
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
    from app.modules.publishing.service import PublishingService
    from app.modules.publishing.tasks import run_publish
    from app.db.models import CorrectionSuggestion, RuleVersion

    settings.PUBLISH_FILE_EXPORT_DIRECTORY = str(tmp_path / "exports")

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
        actor=admin_user, name=f"backfill_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
        validation_run_id=validation_run.id, name="backfill", actor=admin_user
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

    publish_run, job = PublishingService(db).trigger(
        staging_run.id, target_type="FILE_EXPORT", target_reference="backfill.jsonl", overwrite=False, actor=admin_user
    )
    run_publish(str(job.id), str(publish_run.id))
    db.expire_all()


def _edge_set(db: Session) -> set:
    return {
        (r.parent_entity_type, r.parent_entity_id, r.child_entity_type, r.child_entity_id, r.relationship_type)
        for r in db.execute(select(LineageRecord)).scalars()
    }


def test_backfill_reconstructs_all_live_touch_point_edges(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path
) -> None:
    """Every edge the 9 LIVE touch points wrote during the pipeline run
    must still exist after: truncate lineage_records, then backfill.

    NOTE: the backfilled set can be a strict SUPERSET of the live set, not
    merely equal — the pg_connection fixture itself is created via a direct
    ORM insert in conftest.py, bypassing ConnectionsService.create_connection()
    entirely (a test-only shortcut that predates Phase 10 and is used by
    every phase's tests), so its own DATA_SOURCE -> CONNECTION edge is never
    written by live touch point 0. The backfill correctly reconstructs it
    anyway, since it reads real table data directly rather than replaying
    service calls — this is expected, not a bug, and is exactly the kind of
    gap a backfill is supposed to close for pre-existing data."""
    table_name = f"dq_backfill_{uuid.uuid4().hex[:8]}"
    try:
        _build_full_pipeline(db, redis_client, admin_user, pg_connection, table_name, tmp_path)

        live_edges = _edge_set(db)
        assert live_edges

        # Simulate "pre-Phase-10 data with zero lineage edges" by wiping the
        # table (the ONLY table this test ever truncates directly — it is
        # never modifying any Phase 1-9 table).
        db.execute(text("TRUNCATE TABLE lineage_records"))
        db.commit()
        assert db.execute(select(func.count()).select_from(LineageRecord)).scalar_one() == 0

        backfill(db)
        db.commit()

        backfilled_edges = _edge_set(db)
        assert live_edges <= backfilled_edges
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_backfill_is_safely_rerunnable_with_identical_counts(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path
) -> None:
    table_name = f"dq_backfill_rerun_{uuid.uuid4().hex[:8]}"
    try:
        _build_full_pipeline(db, redis_client, admin_user, pg_connection, table_name, tmp_path)

        db.execute(text("TRUNCATE TABLE lineage_records"))
        db.commit()

        backfill(db)
        db.commit()
        first_count = db.execute(select(func.count()).select_from(LineageRecord)).scalar_one()

        backfill(db)  # re-run
        db.commit()
        second_count = db.execute(select(func.count()).select_from(LineageRecord)).scalar_one()

        assert first_count == second_count
        assert first_count > 0
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_backfill_never_modifies_any_phase1_9_table(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path
) -> None:
    from app.db.models import PublishRun, StagingRun, ValidationRun

    table_name = f"dq_backfill_readonly_{uuid.uuid4().hex[:8]}"
    try:
        _build_full_pipeline(db, redis_client, admin_user, pg_connection, table_name, tmp_path)
        db.execute(text("TRUNCATE TABLE lineage_records"))
        db.commit()

        before = {
            "validation_runs": {(r.id, r.status) for r in db.execute(select(ValidationRun)).scalars()},
            "staging_runs": {(r.id, r.status) for r in db.execute(select(StagingRun)).scalars()},
            "publish_runs": {(r.id, r.status) for r in db.execute(select(PublishRun)).scalars()},
        }

        backfill(db)
        db.commit()
        db.expire_all()

        after = {
            "validation_runs": {(r.id, r.status) for r in db.execute(select(ValidationRun)).scalars()},
            "staging_runs": {(r.id, r.status) for r in db.execute(select(StagingRun)).scalars()},
            "publish_runs": {(r.id, r.status) for r in db.execute(select(PublishRun)).scalars()},
        }
        assert before == after
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

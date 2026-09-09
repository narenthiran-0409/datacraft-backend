"""One focused test per Phase 10 instrumentation touch point (0-8): each
proves BOTH correct lineage-edge creation AND that the surrounding phase's
pre-existing behavior/test assertions are completely unaffected. Mirrors
the existing integration test fixtures/pipelines from each phase's own
test file rather than inventing new setup.
"""
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    ApprovalDecision,
    ApprovalDecisionIssue,
    ApprovalRequest,
    Column,
    Connection,
    ConnectionType,
    Correction,
    CorrectionSuggestion,
    DataSource,
    Dataset,
    Issue,
    LineageRecord,
    ProfileRun,
    PublishRun,
    ReviewRun,
    Schema,
    StagingRun,
    User,
    ValidationRun,
)
from app.modules.connections.credential_vault import LocalRedisVaultClient
from app.modules.connections.service import ConnectionsService
from app.modules.data_sources.service import DataSourcesService


def _edge_exists(db: Session, parent_type, parent_id, child_type, child_id, rel) -> bool:
    return db.execute(
        select(LineageRecord).where(
            LineageRecord.parent_entity_type == parent_type, LineageRecord.parent_entity_id == parent_id,
            LineageRecord.child_entity_type == child_type, LineageRecord.child_entity_id == child_id,
            LineageRecord.relationship_type == rel,
        )
    ).scalar_one_or_none() is not None


# --- Touch point 0: Connections -------------------------------------------


def test_touch_point_0_connection_creation_writes_data_source_to_connection_edge(
    db: Session, redis_client, admin_user: User
) -> None:
    ds = DataSourcesService(db).create_data_source(
        actor=admin_user, name=f"TP0DS_{uuid.uuid4().hex[:8]}", description=None, owner_team=None, business_domain=None
    )
    pg_type = db.execute(select(ConnectionType).where(ConnectionType.code == "POSTGRESQL")).scalar_one()
    service = ConnectionsService(db, LocalRedisVaultClient(redis_client, settings.VAULT_LOCAL_ENCRYPTION_KEY))

    connection = service.create_connection(
        actor=admin_user, data_source_id=ds.id, connection_type_id=pg_type.id, name=f"TP0Conn_{uuid.uuid4().hex[:8]}",
        environment="DEV", host="localhost", port=5432, database_name="db1", service_name=None, username="svc",
        credential={"username": "svc", "password": "secret"}, config={},
    )

    # Correct edge created.
    assert _edge_exists(db, "DATA_SOURCE", ds.id, "CONNECTION", connection.id, "DERIVED_FROM")

    # Existing Phase 2 behavior completely unaffected: same assertions as
    # test_create_connection_stores_only_credential_ref.
    assert connection.credential_ref
    assert connection.credential_ref != "secret"
    raw_row = db.execute(
        text("SELECT credential_ref FROM connections WHERE id = :id"), {"id": str(connection.id)}
    ).scalar_one()
    assert raw_row == connection.credential_ref


# --- Touch point 1: Discovery ----------------------------------------------


def test_touch_point_1_discovery_writes_connection_schema_dataset_column_edges(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()

    assert db.get(type(job), job.id).status == "COMPLETED"  # existing Phase 3 behavior unaffected

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    assert schema.is_active is True  # existing Phase 3 assertion unaffected

    users_dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "users")).scalar_one()
    users_id_column = db.execute(
        select(Column).where(Column.dataset_id == users_dataset.id, Column.name == "id")
    ).scalar_one()

    assert _edge_exists(db, "CONNECTION", pg_connection.id, "SCHEMA", schema.id, "DERIVED_FROM")
    assert _edge_exists(db, "SCHEMA", schema.id, "DATASET", users_dataset.id, "DERIVED_FROM")
    assert _edge_exists(db, "DATASET", users_dataset.id, "COLUMN", users_id_column.id, "DERIVED_FROM")


# --- helper: real discovered dataset, for touch points 2+ ------------------


def _discover_own_users_table(db: Session, redis_client, admin_user: User, pg_connection: Connection) -> Dataset:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    job = JobsService(db, redis_client).create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()
    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    return db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "users")).scalar_one()


# --- Touch point 2: Profiling -----------------------------------------------


def test_touch_point_2_start_profiling_writes_dataset_to_profile_run_edge(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.profiling.service import ProfilingService

    dataset = _discover_own_users_table(db, redis_client, admin_user, pg_connection)

    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )

    assert _edge_exists(db, "DATASET", dataset.id, "PROFILE_RUN", profile_run.id, "PROFILED_BY")

    # Existing Phase 4 behavior unaffected.
    assert profile_run.status == "QUEUED"
    assert profile_run.dataset_id == dataset.id
    assert job.job_type == "PROFILE_RUN"


# --- Touch point 3: Validation ----------------------------------------------


def test_touch_point_3_start_validation_writes_dataset_to_validation_run_edge(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.validation.service import ValidationService

    dataset = _discover_own_users_table(db, redis_client, admin_user, pg_connection)

    validation_run, job = ValidationService(db).start_validation(
        actor=admin_user, dataset_id=dataset.id, template_id=None
    )

    assert _edge_exists(db, "DATASET", dataset.id, "VALIDATION_RUN", validation_run.id, "VALIDATED_BY")

    # Existing Phase 5 behavior unaffected.
    assert validation_run.status == "QUEUED"
    assert validation_run.dataset_id == dataset.id
    assert job.job_type == "VALIDATION_RUN"


# --- helper: a disposable table taken through validation, for touch points 4+ ---


def _build_completed_validation_run(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str):
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation

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
        actor=admin_user, name=f"lineage_{uuid.uuid4().hex[:8]}", description=None, category=None,
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
    return db.get(ValidationRun, validation_run.id)


# --- Touch point 4: Review run creation -------------------------------------


def test_touch_point_4_create_from_validation_run_writes_edges(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.review.service import ReviewService

    table_name = f"dq_lineage_tp4_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _build_completed_validation_run(db, redis_client, admin_user, pg_connection, table_name)

        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="tp4", actor=admin_user
        )

        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
        assert len(issues) == 1  # existing Phase 6 behavior unaffected

        assert _edge_exists(db, "VALIDATION_RUN", validation_run.id, "REVIEW_RUN", review_run.id, "DERIVED_FROM")
        for issue in issues:
            assert _edge_exists(db, "REVIEW_RUN", review_run.id, "ISSUE", issue.id, "DERIVED_FROM")

        # Existing Phase 6 behavior unaffected.
        assert review_run.status == "DRAFT"
        assert review_run.validation_run_id == validation_run.id
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# --- Touch point 5: Correction decision -------------------------------------


def test_touch_point_5_first_decision_writes_edge_redecision_does_not_duplicate(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService

    table_name = f"dq_lineage_tp5_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _build_completed_validation_run(db, redis_client, admin_user, pg_connection, table_name)
        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="tp5", actor=admin_user
        )
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()

        decision_service = CorrectionDecisionService(db)
        correction = decision_service.accept(suggestion.id, admin_user)

        assert _edge_exists(db, "ISSUE", issue.id, "CORRECTION", correction.id, "CORRECTED_BY")
        edges_after_first = db.execute(
            select(LineageRecord).where(LineageRecord.parent_entity_id == issue.id, LineageRecord.child_entity_id == correction.id)
        ).scalars().all()
        assert len(edges_after_first) == 1

        # Re-decision: edit the SAME correction row (upsert-on-issue_id) —
        # must NOT create a second edge or change the child entity_id.
        re_edited = decision_service.edit(suggestion.id, "manual-override", admin_user)
        assert re_edited.id == correction.id  # same row, per Phase 6 upsert design (unaffected)

        edges_after_redecision = db.execute(
            select(LineageRecord).where(LineageRecord.parent_entity_id == issue.id, LineageRecord.child_entity_id == correction.id)
        ).scalars().all()
        assert len(edges_after_redecision) == 1  # still exactly one, no duplicate

        # Existing Phase 6 behavior unaffected.
        assert re_edited.status == "EDITED"
        assert re_edited.final_value == "manual-override"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# --- helper: full pipeline through APPROVED, for touch points 6+ -----------


def _build_approved(db: Session, redis_client, admin_user: User, pg_connection: Connection, table_name: str):
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService
    from app.modules.approval.service import ApprovalService

    validation_run = _build_completed_validation_run(db, redis_client, admin_user, pg_connection, table_name)
    review_run = ReviewService(db).create_from_validation_run(
        validation_run_id=validation_run.id, name="tp_approved", actor=admin_user
    )
    SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
    db.expire_all()

    issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
    suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
    correction = CorrectionDecisionService(db).accept(suggestion.id, admin_user)
    db.expire_all()

    approval_request = ApprovalService(db).submit(review_run.id, admin_user)
    approval_request = ApprovalService(db).decide(
        approval_request.id, decision="APPROVE", issue_ids=[issue.id], comment=None, actor=admin_user
    )
    db.expire_all()
    return review_run, approval_request, correction


# --- Touch point 6: Approval submission -------------------------------------


def test_touch_point_6_submit_writes_correction_to_approval_request_edges(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService
    from app.modules.approval.service import ApprovalService

    table_name = f"dq_lineage_tp6_{uuid.uuid4().hex[:8]}"
    try:
        validation_run = _build_completed_validation_run(db, redis_client, admin_user, pg_connection, table_name)
        review_run = ReviewService(db).create_from_validation_run(
            validation_run_id=validation_run.id, name="tp6", actor=admin_user
        )
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issue = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().first()
        suggestion = db.execute(select(CorrectionSuggestion).where(CorrectionSuggestion.issue_id == issue.id)).scalars().first()
        correction = CorrectionDecisionService(db).accept(suggestion.id, admin_user)
        db.expire_all()

        approval_request = ApprovalService(db).submit(review_run.id, admin_user)

        assert _edge_exists(db, "CORRECTION", correction.id, "APPROVAL_REQUEST", approval_request.id, "APPROVED_BY")

        # Existing Phase 7 behavior unaffected.
        assert approval_request.status == "PENDING"
        assert approval_request.affected_issue_count == 1
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_touch_point_6_two_approval_cycles_produce_distinct_edge_sets(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    """Mirrors Phase 8's own Finding 4 precedent: two approval cycles on one
    review run must produce two distinct CORRECTION -> APPROVAL_REQUEST edge
    sets, covering different approval_request child IDs, not a collision."""
    from app.modules.review.decision_service import CorrectionDecisionService
    from app.modules.review.service import ReviewService
    from app.modules.review.suggestion_service import SuggestionService
    from app.modules.rules.service import RuleAssignmentService, RulesService
    from app.modules.validation.service import ValidationService
    from app.modules.validation.tasks import run_validation
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService
    from app.modules.profiling.service import ProfilingService
    from app.modules.profiling.tasks import run_profile
    from app.modules.approval.service import ApprovalService

    table_name = f"dq_lineage_tp6b_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, a TEXT)"))
        db.execute(text(f"INSERT INTO {table_name} VALUES (1, NULL), (2, NULL)"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        job = JobsService(db, redis_client).create(
            job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
        )
        run_discovery(str(job.id))
        db.expire_all()
        schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
        dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
        col = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "a")).scalar_one()
        profile_run, profile_job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
        run_profile(str(profile_job.id), str(profile_run.id))
        db.expire_all()
        rule = RulesService(db).create_rule(
            actor=admin_user, name=f"tp6b_{uuid.uuid4().hex[:8]}", description=None, category=None,
            rule_type="COMPLETENESS", origin="CUSTOM", definition={"max_null_percentage": 0}, severity="HIGH",
            error_message_template=None,
        )
        version = RulesService(db).list_versions(rule.id)[0]
        RuleAssignmentService(db).create_assignment(
            actor=admin_user, rule_version_id=version.id, dataset_id=dataset.id,
            assignment_scope="SINGLE_COLUMN", column_id=col.id, column_ids=None, template_id=None,
        )
        validation_run, validation_job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(validation_job.id), str(validation_run.id))
        db.expire_all()
        review_run = ReviewService(db).create_from_validation_run(validation_run_id=validation_run.id, name="tp6b", actor=admin_user)
        SuggestionService(db).generate_for_review_run(review_run.id, admin_user)
        db.expire_all()

        issues = db.execute(select(Issue).where(Issue.review_run_id == review_run.id)).scalars().all()
        assert len(issues) == 2
        decision_service = CorrectionDecisionService(db)

        correction_1 = decision_service.correct_directly(issues[0].id, "manual-value-1", admin_user)
        db.expire_all()
        request_1 = ApprovalService(db).submit(review_run.id, admin_user)
        ApprovalService(db).decide(request_1.id, decision="APPROVE", issue_ids=[issues[0].id], comment=None, actor=admin_user)
        db.expire_all()

        db.execute(text("UPDATE review_runs SET status = 'IN_REVIEW' WHERE id = :rid"), {"rid": review_run.id})
        db.commit()
        db.expire_all()

        correction_2 = decision_service.correct_directly(issues[1].id, "manual-value-2", admin_user)
        db.expire_all()
        request_2 = ApprovalService(db).submit(review_run.id, admin_user)
        db.expire_all()

        assert request_2.id != request_1.id
        # Cycle 1's submit() scope was issue[0] only (issue[1] not yet
        # resolved) -> correction_1 -> request_1, and NOT correction_1 ->
        # request_2's sibling correction_2 (didn't exist yet).
        assert _edge_exists(db, "CORRECTION", correction_1.id, "APPROVAL_REQUEST", request_1.id, "APPROVED_BY")
        assert not _edge_exists(db, "CORRECTION", correction_2.id, "APPROVAL_REQUEST", request_1.id, "APPROVED_BY")
        # Cycle 2's submit() scope is the FULL current resolved scope (per
        # ApprovalService._resolved_scope's own definition, and Phase 8's
        # own "second cycle uses the full scope" precedent) -> BOTH
        # corrections now link to request_2. This is the "two distinct
        # edge sets covering different approval_request child IDs" the
        # spec describes -- request_1's set is {correction_1}, request_2's
        # is {correction_1, correction_2} -- not a merge/overwrite of one
        # edge set into the other (both persist independently).
        assert _edge_exists(db, "CORRECTION", correction_1.id, "APPROVAL_REQUEST", request_2.id, "APPROVED_BY")
        assert _edge_exists(db, "CORRECTION", correction_2.id, "APPROVAL_REQUEST", request_2.id, "APPROVED_BY")
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# --- Touch point 7: Staging trigger -----------------------------------------


def test_touch_point_7_trigger_writes_approval_request_to_staging_run_edge(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.staging.service import StagingService

    table_name = f"dq_lineage_tp7_{uuid.uuid4().hex[:8]}"
    try:
        review_run, approval_request, _ = _build_approved(db, redis_client, admin_user, pg_connection, table_name)

        staging_run = StagingService(db).trigger(review_run.id, admin_user)

        assert _edge_exists(db, "APPROVAL_REQUEST", approval_request.id, "STAGING_RUN", staging_run.id, "STAGED_INTO")

        # Existing Phase 8 behavior unaffected.
        assert staging_run.status == "READY"
        assert staging_run.attempt_number == 1
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_touch_point_7_failed_staging_run_still_produces_attempted_edge(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    from app.modules.staging import service as staging_service_module
    from app.modules.staging.service import StagingService
    from app.source_adapters.exceptions import SourceUnreachableError

    table_name = f"dq_lineage_tp7b_{uuid.uuid4().hex[:8]}"
    try:
        review_run, approval_request, _ = _build_approved(db, redis_client, admin_user, pg_connection, table_name)

        def _failing_get_provider(*args, **kwargs):
            raise SourceUnreachableError("simulated source outage")

        monkeypatch.setattr(staging_service_module, "get_provider", _failing_get_provider)

        staging_run = StagingService(db).trigger(review_run.id, admin_user)
        assert staging_run.status == "FAILED"  # existing Phase 8 behavior unaffected

        assert _edge_exists(db, "APPROVAL_REQUEST", approval_request.id, "STAGING_RUN", staging_run.id, "STAGED_INTO")
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


# --- Touch point 8: Publish trigger -----------------------------------------


def test_touch_point_8_trigger_writes_staging_run_to_publish_run_edge(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, tmp_path, monkeypatch
) -> None:
    from app.modules.staging.service import StagingService
    from app.modules.publishing.service import PublishingService

    monkeypatch.setattr(settings, "PUBLISH_FILE_EXPORT_DIRECTORY", str(tmp_path / "exports"))
    table_name = f"dq_lineage_tp8_{uuid.uuid4().hex[:8]}"
    try:
        review_run, approval_request, _ = _build_approved(db, redis_client, admin_user, pg_connection, table_name)
        staging_run = StagingService(db).trigger(review_run.id, admin_user)
        assert staging_run.status == "READY"

        publish_run, job = PublishingService(db).trigger(
            staging_run.id, target_type="FILE_EXPORT", target_reference="tp8.jsonl", overwrite=False, actor=admin_user
        )

        assert _edge_exists(db, "STAGING_RUN", staging_run.id, "PUBLISH_RUN", publish_run.id, "PUBLISHED_TO")

        # Existing Phase 9 behavior unaffected.
        assert publish_run.status == "PENDING"
        assert job.job_type == "PUBLISH"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_touch_point_1_rediscovery_does_not_duplicate_edges(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    jobs_service = JobsService(db, redis_client)
    job1 = jobs_service.create(job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id)
    run_discovery(str(job1.id))
    db.expire_all()
    job2 = jobs_service.create(job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id)
    run_discovery(str(job2.id))
    db.expire_all()

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    edges = db.execute(
        select(LineageRecord).where(
            LineageRecord.parent_entity_type == "CONNECTION", LineageRecord.parent_entity_id == pg_connection.id,
            LineageRecord.child_entity_type == "SCHEMA", LineageRecord.child_entity_id == schema.id,
        )
    ).scalars().all()
    assert len(edges) == 1

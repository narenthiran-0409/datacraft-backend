"""Discovery integration tests, run against this project's own local
Postgres database (via the pg_connection fixture) — a real, reachable
target that conveniently already has interesting tables to discover:
composite PKs (user_roles, role_permissions, dataset_key_columns) and
single-column PKs (users, roles, ...).

The Celery task is invoked directly (not via .delay()/a broker), per the
approved plan: "Direct Celery task invocation is acceptable for automated
tests." Since the task opens its own DB session internally, the test's own
`db` session must be expired after each direct call so it re-reads fresh
rows rather than serving stale cached state.
"""
import uuid

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Column, Connection, Dataset, DatasetKeyColumn, Job, Schema, User
from app.modules.datasets.key_service import DatasetKeyService
from app.modules.discovery import tasks as discovery_tasks
from app.modules.discovery.tasks import run_discovery
from app.modules.jobs.service import JobsService
from app.source_adapters.exceptions import SourceQueryError


def _run_discovery_sync(db: Session, redis_client, connection: Connection, actor: User) -> Job:
    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=connection.id, created_by=actor.id
    )
    run_discovery(str(job.id))
    db.expire_all()
    return db.get(Job, job.id)


def test_discovery_finds_own_tables_with_correct_pk_detection(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    job = _run_discovery_sync(db, redis_client, pg_connection, admin_user)

    assert job.status == "COMPLETED"

    schema = db.execute(
        select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
    ).scalar_one()
    assert schema.is_active is True

    users_dataset = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "users")
    ).scalar_one()
    assert users_dataset.key_strategy == "SINGLE_COLUMN"
    users_key_columns = db.execute(
        select(Column.name)
        .join(DatasetKeyColumn, DatasetKeyColumn.column_id == Column.id)
        .where(DatasetKeyColumn.dataset_id == users_dataset.id)
    ).scalars().all()
    assert users_key_columns == ["id"]
    users_id_column = db.execute(
        select(Column).where(Column.dataset_id == users_dataset.id, Column.name == "id")
    ).scalar_one()
    assert users_id_column.is_primary_key is True
    assert users_dataset.column_count == db.execute(
        select(Column).where(Column.dataset_id == users_dataset.id, Column.is_active.is_(True))
    ).scalars().all().__len__()

    user_roles_dataset = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "user_roles")
    ).scalar_one()
    assert user_roles_dataset.key_strategy == "COMPOSITE"
    user_roles_key_rows = db.execute(
        select(Column.name, DatasetKeyColumn.ordinal)
        .join(DatasetKeyColumn, DatasetKeyColumn.column_id == Column.id)
        .where(DatasetKeyColumn.dataset_id == user_roles_dataset.id)
        .order_by(DatasetKeyColumn.ordinal)
    ).all()
    assert [name for name, _ in user_roles_key_rows] == ["user_id", "role_id"]
    assert [ordinal for _, ordinal in user_roles_key_rows] == [0, 1]


def test_discovery_is_idempotent_on_rerun(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    _run_discovery_sync(db, redis_client, pg_connection, admin_user)

    schema = db.execute(
        select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
    ).scalar_one()
    users_dataset_before = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "users")
    ).scalar_one()
    first_discovered_at = users_dataset_before.discovered_at
    first_created_at = users_dataset_before.created_at
    first_dataset_id = users_dataset_before.id
    first_column_count = users_dataset_before.column_count

    _run_discovery_sync(db, redis_client, pg_connection, admin_user)

    users_dataset_after = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "users")
    ).scalar_one()
    assert users_dataset_after.id == first_dataset_id
    assert users_dataset_after.created_at == first_created_at
    assert users_dataset_after.discovered_at > first_discovered_at
    assert users_dataset_after.column_count == first_column_count
    assert users_dataset_after.is_active is True


def test_rediscovery_lifecycle_with_disposable_schema(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    schema_name = f"dq_test_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE SCHEMA {schema_name}"))
        db.execute(text(f"CREATE TABLE {schema_name}.widget (id INT PRIMARY KEY, a TEXT, b TEXT)"))
        db.commit()

        _run_discovery_sync(db, redis_client, pg_connection, admin_user)

        pg_schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == schema_name)
        ).scalar_one()
        widget = db.execute(
            select(Dataset).where(Dataset.schema_id == pg_schema.id, Dataset.name == "widget")
        ).scalar_one()
        assert widget.is_active is True
        assert widget.column_count == 3
        col_b = db.execute(select(Column).where(Column.dataset_id == widget.id, Column.name == "b")).scalar_one()
        assert col_b.is_active is True

        # --- column removed ---
        db.execute(text(f"ALTER TABLE {schema_name}.widget DROP COLUMN b"))
        db.commit()
        _run_discovery_sync(db, redis_client, pg_connection, admin_user)

        db.expire_all()
        widget = db.get(Dataset, widget.id)
        assert widget.column_count == 2
        col_b = db.execute(select(Column).where(Column.dataset_id == widget.id, Column.name == "b")).scalar_one()
        assert col_b.is_active is False
        col_id = db.execute(select(Column).where(Column.dataset_id == widget.id, Column.name == "id")).scalar_one()
        assert col_id.is_primary_key is True

        # --- table dropped ---
        db.execute(text(f"DROP TABLE {schema_name}.widget"))
        db.commit()
        _run_discovery_sync(db, redis_client, pg_connection, admin_user)

        db.expire_all()
        widget = db.get(Dataset, widget.id)
        assert widget.is_active is False

        # --- table recreated: reactivated, not duplicated ---
        db.execute(text(f"CREATE TABLE {schema_name}.widget (id INT PRIMARY KEY, a TEXT, b TEXT)"))
        db.commit()
        _run_discovery_sync(db, redis_client, pg_connection, admin_user)

        db.expire_all()
        matching_datasets = db.execute(
            select(Dataset).where(Dataset.schema_id == pg_schema.id, Dataset.name == "widget")
        ).scalars().all()
        assert len(matching_datasets) == 1
        assert matching_datasets[0].id == widget.id
        assert matching_datasets[0].is_active is True
    finally:
        db.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        db.commit()


def test_manual_key_column_configuration_for_pk_less_table(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    schema_name = f"dq_test_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE SCHEMA {schema_name}"))
        db.execute(text(f"CREATE TABLE {schema_name}.pkless (external_id TEXT, note TEXT)"))
        db.commit()

        _run_discovery_sync(db, redis_client, pg_connection, admin_user)

        pg_schema = db.execute(
            select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == schema_name)
        ).scalar_one()
        dataset = db.execute(
            select(Dataset).where(Dataset.schema_id == pg_schema.id, Dataset.name == "pkless")
        ).scalar_one()
        assert dataset.key_strategy == "ROW_INDEX_FALLBACK"
        assert (
            db.execute(select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id)).scalars().first()
            is None
        )

        external_id_column = db.execute(
            select(Column).where(Column.dataset_id == dataset.id, Column.name == "external_id")
        ).scalar_one()

        key_service = DatasetKeyService(db)
        updated_dataset = key_service.set_key_columns(
            actor=admin_user,
            dataset_id=dataset.id,
            key_columns=[{"column_id": external_id_column.id, "ordinal": 0}],
        )

        assert updated_dataset.key_strategy == "SINGLE_COLUMN"
        db.refresh(external_id_column)
        assert external_id_column.is_primary_key is True
    finally:
        db.execute(text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        db.commit()


def test_discovery_completed_audit_event_uses_job_created_by_as_actor(
    db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    job = _run_discovery_sync(db, redis_client, pg_connection, admin_user)
    assert job.status == "COMPLETED"

    event = db.execute(
        select(AuditEvent).where(AuditEvent.action == "discovery.completed", AuditEvent.entity_id == pg_connection.id)
    ).scalars().first()
    assert event is not None
    assert event.actor_id == admin_user.id
    assert event.actor_type == "USER"
    assert event.entity_type == "CONNECTION"


def test_per_dataset_failure_writes_dataset_failed_audit_and_continues(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """A single dataset failing (SourceQueryError) must not abort the whole
    run: it's caught, audited as discovery.dataset_failed with
    entity_type='CONNECTION' (no dataset row may exist yet) and a valid
    entity_id, and the run continues to completion for the rest."""
    real_get_provider = discovery_tasks.get_provider

    class FailingColumnsWrapper:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_columns(self, schema, dataset):
            if dataset == "roles":
                raise SourceQueryError("simulated failure for 'roles'")
            return self._inner.get_columns(schema, dataset)

    def fake_get_provider(*args, **kwargs):
        return FailingColumnsWrapper(real_get_provider(*args, **kwargs))

    monkeypatch.setattr(discovery_tasks, "get_provider", fake_get_provider)

    job = _run_discovery_sync(db, redis_client, pg_connection, admin_user)
    assert job.status == "COMPLETED"
    assert job.error_message is not None
    assert "1 failed" in job.error_message

    failed_event = db.execute(
        select(AuditEvent).where(
            AuditEvent.action == "discovery.dataset_failed", AuditEvent.entity_id == pg_connection.id
        )
    ).scalars().first()
    assert failed_event is not None
    assert failed_event.entity_type == "CONNECTION"
    assert failed_event.audit_metadata["dataset"] == "roles"

    # every other table still got discovered
    schema = db.execute(
        select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
    ).scalar_one()
    users_dataset = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "users")
    ).scalar_one_or_none()
    assert users_dataset is not None

    # the failed dataset itself was never created (failed before its row existed)
    roles_dataset = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == "roles")
    ).scalar_one_or_none()
    assert roles_dataset is None

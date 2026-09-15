"""Phase 4.6 integration tests, run against this project's own local
Postgres database (via the pg_connection fixture) — the only provider
this project has ever live-verified (mirrors tests/integration/test_preview.py's
own stated policy).
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Connection, Dataset, DatasetKeyColumn, Schema, User


def _discover(db: Session, redis_client, connection: Connection, actor: User) -> None:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=connection.id, created_by=actor.id
    )
    run_discovery(str(job.id))
    db.expire_all()


def _make_table_and_discover(db, redis_client, admin_user, pg_connection, ddl: str, rows_sql: str):
    table_name = f"dq_bk_{uuid.uuid4().hex[:8]}"
    db.execute(text(ddl.format(table=table_name)))
    if rows_sql:
        db.execute(text(rows_sql.format(table=table_name)))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    _discover(db, redis_client, pg_connection, admin_user)

    schema = db.execute(select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")).scalar_one()
    dataset = db.execute(select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)).scalar_one()
    return table_name, dataset


def test_discovery_recommends_verified_candidate_for_no_pk_unique_column(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",  # no PK -> ROW_INDEX_FALLBACK
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b'), (3, 'c')",
    )
    try:
        assert dataset.key_strategy == "ROW_INDEX_FALLBACK"
        response = client.get(f"/api/v1/datasets/{dataset.id}/business-key/candidates", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["already_has_reliable_key"] is False
        assert body["status"] == "VERIFIED_UNIQUE"
        assert body["recommended"]["columns"] == ["order_no"]
        assert body["is_full_scan"] is True

        # Detection must NEVER mutate dataset identity.
        db.expire_all()
        refreshed = db.get(Dataset, dataset.id)
        assert refreshed.key_strategy == "ROW_INDEX_FALLBACK"
        key_cols = db.execute(select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id)).scalars().all()
        assert key_cols == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_discovery_rejects_duplicate_id_like_column_generically(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    """Generic reproduction of the real Customer_Orders shape: an id-like
    column with one duplicate value must be naturally rejected — no
    special-casing of any column name."""
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (some_ref INT, other_col TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b'), (2, 'c'), (3, 'd')",
    )
    try:
        response = client.get(f"/api/v1/datasets/{dataset.id}/business-key/candidates", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "NO_CANDIDATE"
        rejected = [e for e in body["candidates"]]  # candidates list empty when NO_CANDIDATE
        assert rejected == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_discovery_skips_when_dataset_already_has_reliable_key(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (id INT PRIMARY KEY, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b')",
    )
    try:
        assert dataset.key_strategy == "SINGLE_COLUMN"
        response = client.get(f"/api/v1/datasets/{dataset.id}/business-key/candidates", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["already_has_reliable_key"] is True
        assert body["status"] is None
        assert body["recommended"] is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_confirm_rejects_when_no_longer_verified(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b')",
    )
    try:
        # Data changes (a duplicate appears) between discovery and confirmation.
        db.execute(text(f"INSERT INTO {table_name} VALUES (2, 'c')"))
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        response = client.post(
            f"/api/v1/datasets/{dataset.id}/business-key/confirm", headers=admin_headers,
            json={"columns": ["order_no"]},
        )
        assert response.status_code == 422
        db.expire_all()
        refreshed = db.get(Dataset, dataset.id)
        assert refreshed.key_strategy == "ROW_INDEX_FALLBACK"
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_confirm_rejects_stale_column_mismatch(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b')",
    )
    try:
        response = client.post(
            f"/api/v1/datasets/{dataset.id}/business-key/confirm", headers=admin_headers,
            json={"columns": ["name"]},  # not the actual verified candidate (order_no)
        )
        assert response.status_code == 422
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_confirm_succeeds_for_genuine_verified_candidate_and_reuses_declared_key_representation(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b'), (3, 'c')",
    )
    try:
        response = client.post(
            f"/api/v1/datasets/{dataset.id}/business-key/confirm", headers=admin_headers,
            json={"columns": ["order_no"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["key_strategy"] == "SINGLE_COLUMN"

        db.expire_all()
        key_cols = db.execute(
            select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id)
        ).scalars().all()
        assert len(key_cols) == 1

        audit_events = db.execute(
            select(AuditEvent).where(
                AuditEvent.entity_type == "DATASET", AuditEvent.entity_id == dataset.id,
                AuditEvent.action == "dataset.business_key_confirmed",
            )
        ).scalars().all()
        assert len(audit_events) == 1
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_profiling_never_automatically_adopts_a_business_key(
    db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b'), (3, 'c')",
    )
    try:
        from app.modules.profiling.service import ProfilingService
        from app.modules.profiling.tasks import run_profile

        profile_run, job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=True,
        )
        run_profile(str(job.id), str(profile_run.id))
        db.expire_all()

        refreshed = db.get(Dataset, dataset.id)
        assert refreshed.key_strategy == "ROW_INDEX_FALLBACK"
        key_cols = db.execute(select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id)).scalars().all()
        assert key_cols == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_validation_never_automatically_adopts_a_business_key(
    db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b'), (3, 'c')",
    )
    try:
        from app.modules.validation.service import ValidationService
        from app.modules.validation.tasks import run_validation

        validation_run, job = ValidationService(db).start_validation(actor=admin_user, dataset_id=dataset.id, template_id=None)
        run_validation(str(job.id), str(validation_run.id))
        db.expire_all()

        refreshed = db.get(Dataset, dataset.id)
        assert refreshed.key_strategy == "ROW_INDEX_FALLBACK"
        key_cols = db.execute(select(DatasetKeyColumn).where(DatasetKeyColumn.dataset_id == dataset.id)).scalars().all()
        assert key_cols == []
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_confirm_requires_metadata_manage_permission(
    client: TestClient, no_role_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection,
) -> None:
    table_name, dataset = _make_table_and_discover(
        db, redis_client, admin_user, pg_connection,
        ddl="CREATE TABLE {table} (order_no INT, name TEXT)",
        rows_sql="INSERT INTO {table} VALUES (1, 'a'), (2, 'b')",
    )
    try:
        response = client.post(
            f"/api/v1/datasets/{dataset.id}/business-key/confirm", headers=no_role_headers,
            json={"columns": ["order_no"]},
        )
        assert response.status_code == 403

        response = client.get(f"/api/v1/datasets/{dataset.id}/business-key/candidates", headers=no_role_headers)
        assert response.status_code == 403
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

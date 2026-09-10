"""Data Preview integration tests, run against this project's own local
Postgres database (via the pg_connection fixture) — the only provider this
project has ever live-verified. SQL Server/MySQL/Oracle/SAP HANA's
sample_rows() are mock-only (see their respective tests/unit files) and
are NOT exercised here.
"""
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, Connection, Dataset, Schema, User
from app.modules.datasets import preview_service as preview_service_module
from app.source_adapters.exceptions import SourceUnreachableError


def _discover(db: Session, redis_client, connection: Connection, actor: User) -> None:
    from app.modules.discovery.tasks import run_discovery
    from app.modules.jobs.service import JobsService

    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=connection.id, created_by=actor.id
    )
    run_discovery(str(job.id))
    db.expire_all()


def _make_preview_table(db: Session, redis_client, admin_user: User, pg_connection: Connection):
    table_name = f"dq_preview_{uuid.uuid4().hex[:8]}"
    long_value = "x" * 250
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, note TEXT)"))
    db.execute(text(f"INSERT INTO {table_name} VALUES (1, '{long_value}'), (2, NULL), (3, 'short')"))
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    _discover(db, redis_client, pg_connection, admin_user)

    schema = db.execute(
        select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == "public")
    ).scalar_one()
    dataset = db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)
    ).scalar_one()

    return table_name, dataset


def test_preview_returns_live_rows_from_source(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        response = client.get(f"/api/v1/datasets/{dataset.id}/preview", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["dataset_id"] == str(dataset.id)
        assert body["schema_name"] == "public"
        assert body["table_name"] == table_name
        assert body["row_count"] == 3
        assert set(body["columns"]) == {"id", "note"}
        assert body["capped_to_max"] is False
        ids = sorted(row["id"] for row in body["rows"])
        assert ids == [1, 2, 3]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_preview_truncates_long_string_values(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        response = client.get(f"/api/v1/datasets/{dataset.id}/preview", headers=admin_headers)
        body = response.json()
        row_with_long_value = next(row for row in body["rows"] if row["id"] == 1)
        assert len(row_with_long_value["note"]) == 100

        row_with_null = next(row for row in body["rows"] if row["id"] == 2)
        assert row_with_null["note"] is None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_preview_row_count_is_clamped_to_server_max(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        response = client.get(
            f"/api/v1/datasets/{dataset.id}/preview", headers=admin_headers, params={"row_count": 5000}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["requested_row_count"] == 5000
        assert body["capped_to_max"] is True
        # The table only has 3 rows, but proves the request wasn't rejected
        # with a 422 the way Profiling's sample_size would be — clamped, not refused.
        assert body["row_count"] == 3
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_preview_nonexistent_dataset_returns_404(client: TestClient, admin_headers: dict) -> None:
    response = client.get(f"/api/v1/datasets/{uuid.uuid4()}/preview", headers=admin_headers)
    assert response.status_code == 404


def test_preview_inactive_dataset_returns_409(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        dataset.is_active = False
        db.commit()

        response = client.get(f"/api/v1/datasets/{dataset.id}/preview", headers=admin_headers)
        assert response.status_code == 409
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_analyst_can_preview_dataset(
    client: TestClient,
    admin_headers: dict,
    analyst_headers: dict,
    db: Session,
    redis_client,
    admin_user: User,
    pg_connection: Connection,
) -> None:
    """data_preview.read is granted to every role, including analyst — see
    migration 0018's docstring for the reasoning."""
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        response = client.get(f"/api/v1/datasets/{dataset.id}/preview", headers=analyst_headers)
        assert response.status_code == 200
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_preview_writes_audit_event_without_raw_values(
    client: TestClient, admin_headers: dict, db: Session, redis_client, admin_user: User, pg_connection: Connection
) -> None:
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        client.get(f"/api/v1/datasets/{dataset.id}/preview", headers=admin_headers)

        event = db.execute(
            select(AuditEvent).where(AuditEvent.action == "dataset.previewed", AuditEvent.entity_id == dataset.id)
        ).scalar_one()
        assert event.entity_type == "DATASET"
        assert event.audit_metadata["returned_row_count"] == 3
        assert event.audit_metadata["schema"] == "public"
        assert event.audit_metadata["table"] == table_name
        # Never the actual row content, only counts/identifiers.
        serialized_metadata = str(event.audit_metadata)
        assert "x" * 250 not in serialized_metadata
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_preview_source_unreachable_returns_502_and_audits_failure(
    client: TestClient,
    admin_headers: dict,
    db: Session,
    redis_client,
    admin_user: User,
    pg_connection: Connection,
    monkeypatch,
) -> None:
    """Mirrors Discovery/Profiling's own terminal-error simulation pattern
    (tests/integration/test_discovery.py) — wrap the real provider with a
    fake that raises a categorized source_adapters exception, and confirm
    it surfaces as a controlled error response, not a raw 500."""
    table_name, dataset = _make_preview_table(db, redis_client, admin_user, pg_connection)
    try:
        real_get_provider = preview_service_module.get_provider

        class FailingProvider:
            def sample_rows(self, *args, **kwargs):
                raise SourceUnreachableError("simulated: could not reach host")

            def close(self):
                pass

        def fake_get_provider(*args, **kwargs):
            real_get_provider(*args, **kwargs).close()
            return FailingProvider()

        monkeypatch.setattr(preview_service_module, "get_provider", fake_get_provider)

        response = client.get(f"/api/v1/datasets/{dataset.id}/preview", headers=admin_headers)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "PREVIEW_SOURCE_UNAVAILABLE"

        failed_event = db.execute(
            select(AuditEvent).where(
                AuditEvent.action == "dataset.preview_failed", AuditEvent.entity_id == dataset.id
            )
        ).scalar_one()
        assert "SourceUnreachableError" in failed_event.audit_metadata["error"]
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()

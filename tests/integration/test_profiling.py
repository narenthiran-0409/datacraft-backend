"""Profiling integration tests, run against real local Postgres via
purpose-built disposable tables with known, deliberately-constructed
characteristics. The Celery task is invoked directly (not via .delay()), per
the approved plan: direct Celery task invocation is acceptable for
automated tests. Since the task opens its own DB session internally, the
test's own `db` session is expired after each direct call to avoid serving
stale cached state.
"""
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import FullScanExceedsLimitError, SampleSizeExceedsLimitError
from app.db.models import AuditEvent, Column, ColumnProfile, Connection, Dataset, Job, ProfileRun, Schema, User
from app.modules.discovery.tasks import run_discovery
from app.modules.jobs.service import JobsService
from app.modules.profiling.service import ProfilingService
from app.modules.profiling.tasks import run_profile

_PUBLIC_SCHEMA_NAME = "public"


def _discover(db: Session, redis_client, admin_user: User, pg_connection: Connection) -> None:
    jobs_service = JobsService(db, redis_client)
    job = jobs_service.create(
        job_type="DISCOVERY_RUN", entity_type="CONNECTION", entity_id=pg_connection.id, created_by=admin_user.id
    )
    run_discovery(str(job.id))
    db.expire_all()


def _get_dataset(db: Session, pg_connection: Connection, table_name: str) -> Dataset:
    schema = db.execute(
        select(Schema).where(Schema.connection_id == pg_connection.id, Schema.name == _PUBLIC_SCHEMA_NAME)
    ).scalar_one()
    return db.execute(
        select(Dataset).where(Dataset.schema_id == schema.id, Dataset.name == table_name)
    ).scalar_one()


def _start_and_run(
    db: Session,
    redis_client,
    admin_user: User,
    dataset: Dataset,
    *,
    sample_size: int | None = None,
    full_scan: bool = False,
    include_top_values: bool = False,
) -> ProfileRun:
    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user,
        dataset_id=dataset.id,
        sample_size=sample_size,
        full_scan=full_scan,
        include_top_values=include_top_values,
    )
    run_profile(str(job.id), str(profile_run.id))
    db.expire_all()
    return db.get(ProfileRun, profile_run.id)


@pytest.fixture
def dup_test_table(db: Session, pg_connection: Connection):
    """A disposable, PK-less 10-row table: rows 1-2 are an exact full-row
    duplicate, rows 3-4 are another exact full-row duplicate, the rest are
    unique -> 2/10 = 20% dataset-level full-row duplicate rate. Also has a
    known-mean numeric column, a string column with an empty string (blank,
    distinct from NULL), a NULL, and a date column."""
    table_name = f"dq_profiling_dup_{uuid.uuid4().hex[:8]}"
    db.execute(
        text(
            f"CREATE TABLE {table_name} "
            "(score NUMERIC, name TEXT, note TEXT, created_on DATE)"
        )
    )
    rows = [
        (10, "Alice", "x", "2024-01-01"),
        (10, "Alice", "x", "2024-01-01"),  # dup of row 1
        (20, "Bob", "y", "2024-02-01"),
        (20, "Bob", "y", "2024-02-01"),  # dup of row 3
        (30, "Carol", "", "2024-03-01"),  # blank note
        (40, "Dave", "w", None),
        (None, "Eve", "v", "2024-05-01"),  # NULL score
        (60, "Frank", "u", "2024-06-01"),
        (70, "Grace", "t", "2024-07-01"),
        (80, "Heidi", "s", "2024-08-01"),
    ]
    for row in rows:
        db.execute(text(f"INSERT INTO {table_name} VALUES (:s, :n, :note, :d)"), {"s": row[0], "n": row[1], "note": row[2], "d": row[3]})
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    yield table_name

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


@pytest.fixture
def large_sample_test_table(db: Session, pg_connection: Connection):
    """A disposable 50-row table for testing sampled-run row_count vs
    sample_size semantics: request a sample much smaller than the table."""
    table_name = f"dq_profiling_sample_{uuid.uuid4().hex[:8]}"
    db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val INT)"))
    for i in range(50):
        db.execute(text(f"INSERT INTO {table_name} VALUES (:i, :v)"), {"i": i, "v": i * 2})
    db.execute(text(f"ANALYZE {table_name}"))
    db.commit()

    yield table_name

    db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
    db.commit()


# ---------------------------------------------------------------------------


def test_row_count_vs_sample_size_for_sampled_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, large_sample_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, large_sample_test_table)
    assert dataset.row_count_estimate is not None and dataset.row_count_estimate >= 40

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, sample_size=10)

    assert profile_run.status == "COMPLETED"
    assert profile_run.sample_size == 10
    # row_count reflects the dataset's row_count_estimate, NOT the sample_size
    assert profile_run.row_count == dataset.row_count_estimate
    assert profile_run.row_count != 10


def test_row_count_equals_sample_size_for_full_scan_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True)

    assert profile_run.status == "COMPLETED"
    assert profile_run.sample_size == 10
    assert profile_run.row_count == 10  # exact, full scan


def test_quality_score_and_null_percentage_always_null(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True)

    assert profile_run.quality_score is None
    assert profile_run.null_percentage is None


def test_dataset_duplicate_percentage_matches_sample(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True)

    assert profile_run.duplicate_percentage is not None
    assert float(profile_run.duplicate_percentage) == 20.00  # 2 of 10 rows are full-row duplicates


def test_full_scan_rejected_over_limit_creates_nothing(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str, monkeypatch
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)
    monkeypatch.setattr(settings, "PROFILING_MAX_FULL_SCAN_ROWS", 1)

    before_count = db.execute(select(ProfileRun)).scalars().all()
    with pytest.raises(FullScanExceedsLimitError):
        ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )
    after_count = db.execute(select(ProfileRun)).scalars().all()
    assert len(after_count) == len(before_count)


def test_oversized_sample_size_rejected_creates_nothing(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    before_count = len(db.execute(select(ProfileRun)).scalars().all())
    with pytest.raises(SampleSizeExceedsLimitError):
        ProfilingService(db, redis_client).start_profiling(
            actor=admin_user,
            dataset_id=dataset.id,
            sample_size=settings.PROFILING_MAX_SAMPLE_SIZE + 1,
            full_scan=False,
            include_top_values=False,
        )
    after_count = len(db.execute(select(ProfileRun)).scalars().all())
    assert after_count == before_count


def test_exact_stats_timeout_fallback_flags_sample_derived(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str, monkeypatch
) -> None:
    """Task-level coverage: the exact-stats call raising ExactStatsTimeoutError
    for any reason (mocked here) is handled correctly by run_profile. The
    provider-level mechanics that actually PRODUCE that exception against a
    real, genuinely slow Postgres query are separately, live-verified by
    test_exact_stats_genuine_postgres_timeout_forces_fallback below."""
    from app.modules.profiling import tasks as profiling_tasks
    from app.source_adapters.exceptions import ExactStatsTimeoutError

    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    real_get_provider = profiling_tasks.get_provider

    class TimeoutStatsWrapper:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def get_dataset_column_stats(self, *args, **kwargs):
            raise ExactStatsTimeoutError("simulated exact-stats timeout")

    monkeypatch.setattr(profiling_tasks, "get_provider", lambda *a, **k: TimeoutStatsWrapper(real_get_provider(*a, **k)))

    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(job.id), str(profile_run.id))
    db.expire_all()
    profile_run = db.get(ProfileRun, profile_run.id)

    assert profile_run.status == "COMPLETED"
    column_profiles = db.execute(select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)).scalars().all()
    assert len(column_profiles) > 0
    for cp in column_profiles:
        assert cp.pattern_summary["exact_stats"] is False
        assert cp.null_count is not None  # sample-derived values still populated


def test_exact_stats_genuine_postgres_timeout_forces_fallback(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, monkeypatch
) -> None:
    """Genuine, real-Postgres-forced timeout (no mocking of the exception
    itself): a real 300,000-row table with a high-cardinality text column
    makes COUNT(DISTINCT ...) take measurably more than an effectively
    unreachable 1ms statement_timeout, so Postgres itself cancels the query
    and PostgreSQLProvider._exact_stats_batch() must translate the real
    QueryCanceled into ExactStatsTimeoutError. This is a test-only setting
    override (monkeypatch), not a production code change/hook.

    Verifies all of: the run still completes; affected columns are flagged
    exact_stats=false; null/distinct counts are still populated (from the
    sample); and no raw driver/timeout text leaks into the task result or
    profile_runs.error_message."""
    table_name = f"dq_genuine_timeout_{uuid.uuid4().hex[:8]}"
    try:
        db.execute(text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT)"))
        db.execute(
            text(f"INSERT INTO {table_name} SELECT i, md5(random()::text) FROM generate_series(1, 300000) i")
        )
        db.execute(text(f"ANALYZE {table_name}"))
        db.commit()

        _discover(db, redis_client, admin_user, pg_connection)
        dataset = _get_dataset(db, pg_connection, table_name)

        monkeypatch.setattr(settings, "PROFILING_EXACT_STATS_TIMEOUT_SECONDS", 0.001)  # genuinely unreachable

        profile_run, job = ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=1000, full_scan=False, include_top_values=False
        )
        result = run_profile(str(job.id), str(profile_run.id))
        db.expire_all()
        profile_run = db.get(ProfileRun, profile_run.id)

        assert result["status"] == "COMPLETED"
        assert profile_run.status == "COMPLETED"

        result_blob = str(result).lower()
        assert "psycopg" not in result_blob
        assert "statement timeout" not in result_blob
        if profile_run.error_message:
            assert "psycopg" not in profile_run.error_message.lower()

        column_profiles = db.execute(
            select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)
        ).scalars().all()
        assert len(column_profiles) == 2  # id, val
        for cp in column_profiles:
            assert cp.pattern_summary["exact_stats"] is False
            assert cp.null_count is not None
            assert cp.distinct_count is not None
    finally:
        db.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        db.commit()


def test_top_values_absent_by_default(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True, include_top_values=False)

    column_profiles = db.execute(select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)).scalars().all()
    assert len(column_profiles) > 0
    for cp in column_profiles:
        assert cp.value_distribution is None


def test_top_values_present_and_capped_when_requested(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True, include_top_values=True)

    name_column = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "name")).scalar_one()
    cp = db.execute(
        select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id, ColumnProfile.column_id == name_column.id)
    ).scalar_one()

    assert cp.value_distribution is not None
    assert len(cp.value_distribution) <= 10
    assert all(len(entry["value"]) <= 100 for entry in cp.value_distribution)


def test_audit_events_contain_no_raw_values_or_forbidden_fields(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    _start_and_run(db, redis_client, admin_user, dataset, full_scan=True, include_top_values=True)

    events = db.execute(
        select(AuditEvent).where(AuditEvent.entity_id == dataset.id, AuditEvent.action.like("profiling.%"))
    ).scalars().all()
    assert len(events) > 0
    forbidden_substrings = ["Alice", "Bob", "quality_score", "null_percentage"]
    for event in events:
        blob = str(event.audit_metadata)
        for forbidden in forbidden_substrings:
            assert forbidden not in blob, f"{forbidden!r} leaked into {event.action} metadata: {blob}"


def test_rerun_creates_new_row_not_update(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    first_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True)
    second_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True)

    assert first_run.id != second_run.id
    all_runs = db.execute(select(ProfileRun).where(ProfileRun.dataset_id == dataset.id)).scalars().all()
    assert len(all_runs) == 2


def test_deactivated_dataset_rejected(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    from app.core.exceptions import DatasetNotActiveError

    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)
    dataset.is_active = False
    db.commit()

    with pytest.raises(DatasetNotActiveError):
        ProfilingService(db, redis_client).start_profiling(
            actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
        )


def test_deactivated_column_is_skipped(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    note_column = db.execute(select(Column).where(Column.dataset_id == dataset.id, Column.name == "note")).scalar_one()
    note_column.is_active = False
    db.commit()

    profile_run = _start_and_run(db, redis_client, admin_user, dataset, full_scan=True)

    profiled_column_ids = {
        cp.column_id
        for cp in db.execute(select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)).scalars()
    }
    assert note_column.id not in profiled_column_ids


def test_generalized_stale_sweep_fails_both_job_and_profile_run(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str, monkeypatch
) -> None:
    from datetime import datetime, timedelta, timezone

    from app.modules.jobs.tasks import sweep_stale_jobs

    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=100, full_scan=False, include_top_values=False
    )
    # Simulate a stuck RUNNING job: no worker ever finished it, updated_at is stale.
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=settings.STALE_JOB_THRESHOLD_MINUTES + 5)
    job.status = "RUNNING"
    job.started_at = stale_time
    job.updated_at = stale_time
    profile_run.status = "RUNNING"
    profile_run.started_at = stale_time
    db.commit()

    sweep_stale_jobs()
    db.expire_all()

    swept_job = db.get(Job, job.id)
    swept_run = db.get(ProfileRun, profile_run.id)
    assert swept_job.status == "FAILED"
    assert swept_run.status == "FAILED"


def test_include_top_values_recoverable_without_task_kwarg(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    """The exact scenario a task-kwarg-only design would fail: run_profile
    is called with ONLY job_id/profile_run_id (no include_top_values kwarg
    at all — its signature doesn't even accept one), simulating a manual
    recovery/re-invocation after the original Celery message was lost
    (e.g. a worker died after acking but before finishing). The value must
    still be correctly recovered from Redis, not silently default to
    False."""
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=True
    )

    # No include_top_values kwarg passed here at all — proves it isn't
    # relied upon, only the Redis-backed value keyed by profile_run_id.
    result = run_profile(str(job.id), str(profile_run.id))
    db.expire_all()

    assert result["status"] == "COMPLETED"
    profile_run = db.get(ProfileRun, profile_run.id)
    column_profiles = db.execute(select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)).scalars().all()
    assert len(column_profiles) > 0
    assert any(cp.value_distribution is not None for cp in column_profiles)  # top-values WERE included


def test_include_top_values_redis_key_cleared_after_completion(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    from app.modules.profiling.run_options import _key

    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=True
    )
    assert redis_client.get(_key(profile_run.id)) is not None  # set at creation

    run_profile(str(job.id), str(profile_run.id))

    assert redis_client.get(_key(profile_run.id)) is None  # cleared on completion, not left to leak


def test_duplicate_celery_delivery_is_idempotent(
    db: Session, redis_client, admin_user: User, pg_connection: Connection, dup_test_table: str
) -> None:
    _discover(db, redis_client, admin_user, pg_connection)
    dataset = _get_dataset(db, pg_connection, dup_test_table)

    profile_run, job = ProfilingService(db, redis_client).start_profiling(
        actor=admin_user, dataset_id=dataset.id, sample_size=None, full_scan=True, include_top_values=False
    )
    run_profile(str(job.id), str(profile_run.id))
    db.expire_all()
    first_pass_column_profile_count = len(
        db.execute(select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)).scalars().all()
    )

    # Redelivered message for the same (already-COMPLETED) job/run.
    result = run_profile(str(job.id), str(profile_run.id))
    db.expire_all()

    assert result["status"] == "COMPLETED"
    second_pass_column_profile_count = len(
        db.execute(select(ColumnProfile).where(ColumnProfile.profile_run_id == profile_run.id)).scalars().all()
    )
    assert second_pass_column_profile_count == first_pass_column_profile_count  # no duplicate rows written

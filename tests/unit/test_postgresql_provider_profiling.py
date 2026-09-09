from unittest.mock import MagicMock

import pytest
from psycopg.errors import QueryCanceled

from app.source_adapters.exceptions import ExactStatsTimeoutError
from app.source_adapters.postgresql_provider import PostgreSQLProvider


def _make_provider() -> PostgreSQLProvider:
    return PostgreSQLProvider(host="localhost", port=5432, database="db", username="u", password="p")


def _wire_mock_connection(provider: PostgreSQLProvider, execute_side_effect=None, fetchone_result=None, fetchall_result=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = fetchone_result
    cur.fetchall.return_value = fetchall_result or []
    if execute_side_effect is not None:
        cur.execute.side_effect = execute_side_effect
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn
    return cur


def test_get_dataset_column_stats_single_batch() -> None:
    provider = _make_provider()
    # total_row_count=100, col_a: non_null=90 distinct=50, col_b: non_null=80 distinct=10
    cur = _wire_mock_connection(provider, fetchone_result=(100, 90, 50, 80, 10))

    result = provider.get_dataset_column_stats("public", "t", ["col_a", "col_b"])

    assert result["col_a"].null_count == 10
    assert result["col_a"].distinct_count == 50
    assert result["col_a"].total_row_count == 100
    assert result["col_b"].null_count == 20
    assert result["col_b"].distinct_count == 10


def test_get_dataset_column_stats_batches_when_over_max_columns(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PROFILING_EXACT_STATS_MAX_COLUMNS_PER_QUERY", 2)
    provider = _make_provider()

    call_count = {"n": 0}

    def fetchone_side_effect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            return (100, 90, 50, 80, 10)  # batch 1: col_a, col_b
        return (100, 70, 5)  # batch 2: col_c

    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = fetchone_side_effect
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    result = provider.get_dataset_column_stats("public", "t", ["col_a", "col_b", "col_c"])

    assert set(result.keys()) == {"col_a", "col_b", "col_c"}
    assert result["col_c"].null_count == 30
    assert result["col_c"].distinct_count == 5
    # 2 batches -> cursor.execute called for SET timeout + query + SET reset, twice
    assert cur.execute.call_count == 6


def test_get_dataset_column_stats_raises_exact_stats_timeout_on_query_canceled() -> None:
    """Faithfully simulates real Postgres semantics: once QueryCanceled is
    raised, the transaction is aborted and ANY further command on that
    connection (e.g. a naive "reset statement_timeout" attempt) would also
    fail with InFailedSqlTransaction — this mock raises on a second execute
    call to catch exactly that class of bug (a prior version of this code
    tried to reset statement_timeout in a `finally` block after the
    cancelled query, which masked ExactStatsTimeoutError as SourceQueryError
    and left the connection unusable; see postgresql_provider.py's
    _exact_stats_batch for the fix and a live Postgres reproduction in
    tests/integration/test_profiling.py::test_exact_stats_genuine_postgres_timeout_forces_fallback).
    """
    provider = _make_provider()

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        query = args[0] if args else ""
        if call_count["n"] == 1:
            assert isinstance(query, str) and query.startswith("SET statement_timeout")
            return None
        # Second call: the real query. Anything after this should NOT
        # attempt another cur.execute() on this (now aborted) transaction.
        raise QueryCanceled("canceling statement due to statement timeout")

    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.execute.side_effect = side_effect
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    with pytest.raises(ExactStatsTimeoutError):
        provider.get_dataset_column_stats("public", "t", ["col_a"])

    assert cur.execute.call_count == 2  # SET timeout, then the cancelled query — no reset attempt after
    conn.rollback.assert_called_once()  # connection left usable for subsequent calls


def test_sample_rows_full_scan_when_estimate_within_sample_size() -> None:
    provider = _make_provider()
    rows = [{"id": 1}, {"id": 2}]
    cur = _wire_mock_connection(provider, fetchall_result=rows)

    result = provider.sample_rows("public", "t", sample_size=1000, row_count_estimate=2)

    assert result.is_full_scan is True
    assert result.rows == rows
    executed_sql = str(cur.execute.call_args_list[0])
    assert "TABLESAMPLE" not in executed_sql


def test_sample_rows_uses_tablesample_when_estimate_exceeds_sample_size() -> None:
    provider = _make_provider()
    rows = [{"id": i} for i in range(10)]
    cur = _wire_mock_connection(provider, fetchall_result=rows)

    result = provider.sample_rows("public", "t", sample_size=10, row_count_estimate=1000000)

    assert result.is_full_scan is False
    executed_sql = str(cur.execute.call_args_list[0])
    assert "TABLESAMPLE" in executed_sql


def test_sample_rows_falls_back_to_limit_when_no_estimate() -> None:
    provider = _make_provider()
    rows = [{"id": 1}]
    cur = _wire_mock_connection(provider, fetchall_result=rows)

    result = provider.sample_rows("public", "t", sample_size=10, row_count_estimate=None)

    assert result.is_full_scan is False
    executed_sql = str(cur.execute.call_args_list[0])
    assert "TABLESAMPLE" not in executed_sql
    assert "LIMIT" in executed_sql

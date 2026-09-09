from unittest.mock import MagicMock

import pytest

from app.source_adapters.exceptions import SourceQueryError
from app.source_adapters.postgresql_provider import PostgreSQLProvider


def _make_provider() -> PostgreSQLProvider:
    return PostgreSQLProvider(host="localhost", port=5432, database="db", username="u", password="p")


def _wire_mock_connection(provider: PostgreSQLProvider, fetchall_result=None, execute_side_effect=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchall.return_value = fetchall_result or []
    if execute_side_effect is not None:
        cur.execute.side_effect = execute_side_effect
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn
    return cur


def test_fetch_rows_by_keys_returns_empty_list_for_empty_keys() -> None:
    provider = _make_provider()
    assert provider.fetch_rows_by_keys("public", "t", []) == []


def test_fetch_rows_by_keys_issues_exactly_one_batched_query() -> None:
    provider = _make_provider()
    rows = [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]
    cur = _wire_mock_connection(provider, fetchall_result=rows)

    result = provider.fetch_rows_by_keys("public", "t", [{"id": 1}, {"id": 2}, {"id": 3}])

    assert result == rows
    assert cur.execute.call_count == 1  # single batched query, not one per record


def test_fetch_rows_by_keys_uses_row_value_in_syntax_for_composite_keys() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])

    provider.fetch_rows_by_keys("public", "t", [{"region_id": 5, "order_id": 1002}, {"region_id": 6, "order_id": 1003}])

    executed_sql = str(cur.execute.call_args_list[0])
    assert "IN" in executed_sql
    # Params flattened in (key_dict, column) order — 2 records x 2 columns = 4 params.
    params = cur.execute.call_args_list[0].args[1]
    assert params == [5, 1002, 6, 1003]


def test_fetch_rows_by_keys_wraps_driver_error() -> None:
    import psycopg

    provider = _make_provider()
    _wire_mock_connection(provider, execute_side_effect=psycopg.Error("boom"))

    with pytest.raises(SourceQueryError):
        provider.fetch_rows_by_keys("public", "t", [{"id": 1}])

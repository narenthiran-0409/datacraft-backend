from unittest.mock import MagicMock

import pytest

from app.source_adapters.exceptions import SourceQueryError
from app.source_adapters.postgresql_provider import PostgreSQLProvider


def _make_provider() -> PostgreSQLProvider:
    return PostgreSQLProvider(host="localhost", port=5432, database="db", username="u", password="p")


def _wire_mock_connection(provider: PostgreSQLProvider, fetchall_side_effect=None, fetchone_result=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    if fetchall_side_effect is not None:
        cur.fetchall.side_effect = fetchall_side_effect
    cur.fetchone.return_value = fetchone_result
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn
    return cur


def test_count_rows_issues_count_star() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchone_result=(13,))

    assert provider.count_rows("public", "orders") == 13
    assert "COUNT(*)" in str(cur.execute.call_args_list[0])


def test_count_rows_wraps_driver_error() -> None:
    import psycopg

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = psycopg.Error("boom")

    with pytest.raises(SourceQueryError):
        provider.count_rows("public", "orders")


def test_iter_rows_yields_bounded_batches_until_exhausted() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(
        provider,
        fetchall_side_effect=[
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
            [{"id": 3, "name": "c"}],
        ],
    )

    batches = list(provider.iter_rows("public", "t", ["id", "name"], batch_size=2, order_by=["id"]))

    assert batches == [
        [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        [{"id": 3, "name": "c"}],
    ]
    # Second batch had fewer rows than batch_size -> loop stops, no 3rd query.
    assert cur.execute.call_count == 2


def test_iter_rows_empty_table_yields_no_batches() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_side_effect=[[]])

    assert list(provider.iter_rows("public", "t", ["id"], batch_size=100)) == []


def test_iter_rows_uses_offset_increasing_per_batch() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(
        provider, fetchall_side_effect=[[{"id": 1}, {"id": 2}], [{"id": 3}, {"id": 4}], []]
    )

    list(provider.iter_rows("public", "t", ["id"], batch_size=2, order_by=["id"]))

    offsets_used = [call.args[1][1] for call in cur.execute.call_args_list]
    assert offsets_used == [0, 2, 4]


def test_iter_rows_falls_back_to_selected_columns_when_no_order_by() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_side_effect=[[]])

    list(provider.iter_rows("public", "t", ["a", "b"], batch_size=10, order_by=None))

    # psycopg's sql.Composed doesn't stringify to final SQL text without a
    # live connection — assert on the Identifier objects it's built from.
    executed_sql = str(cur.execute.call_args_list[0])
    assert "Identifier('a')" in executed_sql and "Identifier('b')" in executed_sql


def test_iter_rows_wraps_driver_error() -> None:
    import psycopg

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = psycopg.Error("boom")

    with pytest.raises(SourceQueryError):
        list(provider.iter_rows("public", "t", ["id"], batch_size=10))

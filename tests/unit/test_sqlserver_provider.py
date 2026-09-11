from unittest.mock import MagicMock

from app.source_adapters.sqlserver_provider import SQLServerProvider, _normalize_type


def _make_provider() -> SQLServerProvider:
    return SQLServerProvider(host="localhost", port=1433, database="db", username="u", password="p")


def _wire_mock_connection(provider: SQLServerProvider, fetchall_result=None, fetchone_result=None) -> MagicMock:
    cur = MagicMock()
    cur.fetchall.return_value = fetchall_result or []
    cur.fetchone.return_value = fetchone_result
    conn = MagicMock()
    conn.cursor.return_value = cur
    provider._conn = conn
    return cur


def test_get_capabilities() -> None:
    caps = _make_provider().get_capabilities()
    assert caps.supports_foreign_keys is True
    assert caps.supports_exact_row_count is False


def test_normalize_type_mapping() -> None:
    assert _normalize_type("varchar") == "STRING"
    assert _normalize_type("text") == "TEXT"
    assert _normalize_type("int") == "INTEGER"
    assert _normalize_type("decimal") == "DECIMAL"
    assert _normalize_type("date") == "DATE"
    assert _normalize_type("datetime2") == "DATETIME"
    assert _normalize_type("bit") == "BOOLEAN"
    assert _normalize_type("unknown") == "STRING"


def test_list_schemas_excludes_builtin_schemas() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[("dbo",), ("app",)])

    assert provider.list_schemas() == ["dbo", "app"]
    assert "sys" in cur.execute.call_args[0][0]


def test_list_datasets_maps_table_type() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("users", "BASE TABLE"), ("v_users", "VIEW")])

    datasets = provider.list_datasets("dbo")

    assert datasets == [{"name": "users", "object_type": "TABLE"}, {"name": "v_users", "object_type": "VIEW"}]


def test_get_primary_keys_ordered() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("a",), ("b",)])

    assert provider.get_primary_keys("dbo", "t") == ["a", "b"]


def test_get_row_count_none_when_missing() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchone_result=None)

    assert provider.get_row_count("dbo", "t") is None


def test_get_row_count_from_sys_partitions() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchone_result=(4321,))

    assert provider.get_row_count("dbo", "t") == 4321


def test_translate_error_login_failed_maps_to_authentication() -> None:
    from app.source_adapters.exceptions import SourceAuthenticationError

    provider = _make_provider()
    try:
        provider._translate_error(Exception("Login failed for user 'u'"))
        assert False, "expected SourceAuthenticationError"
    except SourceAuthenticationError:
        pass


def test_sample_rows_returns_rows_as_dicts_with_top_clause() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(1, "a"), (2, "b")])
    cur.description = [("id",), ("val",)]

    result = provider.sample_rows("dbo", "orders", sample_size=20)

    assert result.rows == [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]
    assert result.is_full_scan is False
    executed_sql, param = cur.execute.call_args[0]
    assert "SELECT TOP (?)" in executed_sql
    assert "[dbo].[orders]" in executed_sql
    assert param == 20


def test_sample_rows_accepts_row_count_estimate_kwarg() -> None:
    """Regression test: app/modules/validation/tasks.py calls
    provider.sample_rows(..., row_count_estimate=...) unconditionally for
    every provider, but this provider's signature previously only accepted
    (schema, table, sample_size) — the same call from tasks.py would raise
    TypeError before this provider was ever reached (no connection, no
    query). This is exactly the bug that left real SQL Server validation
    runs stuck at RUNNING forever with no error recorded. This provider
    doesn't use the value (no size-aware sampling strategy implemented),
    but must accept it without error."""
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(1, "a")])
    cur.description = [("id",), ("val",)]

    result = provider.sample_rows("dbo", "orders", sample_size=20, row_count_estimate=5)

    assert result.rows == [{"id": 1, "val": "a"}]


def test_sample_rows_quotes_identifiers_containing_brackets() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    provider.sample_rows("weird]schema", "weird]table", sample_size=5)

    executed_sql = cur.execute.call_args[0][0]
    assert "[weird]]schema].[weird]]table]" in executed_sql


def test_sample_rows_wraps_driver_error() -> None:
    import pyodbc
    import pytest

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = pyodbc.Error("boom")

    with pytest.raises(SourceQueryError):
        provider.sample_rows("dbo", "orders", sample_size=20)

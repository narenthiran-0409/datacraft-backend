from unittest.mock import MagicMock

from app.source_adapters.mysql_provider import MySQLProvider, _normalize_type


def _make_provider() -> MySQLProvider:
    return MySQLProvider(host="localhost", port=3306, database="db", username="u", password="p")


def _wire_mock_connection(provider: MySQLProvider, fetchall_result=None, fetchone_result=None) -> MagicMock:
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchall.return_value = fetchall_result or []
    cur.fetchone.return_value = fetchone_result
    conn = MagicMock()
    conn.open = True
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
    assert _normalize_type("tinyint") == "INTEGER"  # MySQL boolean-as-tinyint limitation
    assert _normalize_type("decimal") == "DECIMAL"
    assert _normalize_type("datetime") == "DATETIME"
    assert _normalize_type("unknown") == "STRING"


def test_list_schemas_excludes_system_databases() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[("app_db",)])

    assert provider.list_schemas() == ["app_db"]
    assert "mysql" in cur.execute.call_args[0][0]


def test_get_primary_keys_uses_seq_in_index_order() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("region_id",), ("order_id",)])

    assert provider.get_primary_keys("app_db", "orders") == ["region_id", "order_id"]


def test_get_foreign_keys_filters_null_references() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("owner_id", "app_db", "users", "id")])

    fks = provider.get_foreign_keys("app_db", "orders")

    assert fks == [
        {"column": "owner_id", "referenced_schema": "app_db", "referenced_table": "users", "referenced_column": "id"}
    ]


def test_get_row_count_from_table_rows_estimate() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchone_result=(999,))

    assert provider.get_row_count("app_db", "orders") == 999


def test_translate_error_access_denied_maps_to_authentication() -> None:
    from app.source_adapters.exceptions import SourceAuthenticationError

    provider = _make_provider()
    try:
        provider._translate_error(Exception("(1045, \"Access denied for user 'u'@'host'\")"))
        assert False, "expected SourceAuthenticationError"
    except SourceAuthenticationError:
        pass


def test_sample_rows_returns_rows_as_dicts_with_limit_clause() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(1, "a"), (2, "b")])
    cur.description = [("id",), ("val",)]

    result = provider.sample_rows("app_db", "orders", sample_size=20)

    assert result.rows == [{"id": 1, "val": "a"}, {"id": 2, "val": "b"}]
    assert result.is_full_scan is False
    executed_sql, params = cur.execute.call_args[0]
    assert "LIMIT %s" in executed_sql
    assert "`app_db`.`orders`" in executed_sql
    assert params == (20,)


def test_sample_rows_accepts_row_count_estimate_kwarg() -> None:
    """Regression test: app/modules/validation/tasks.py calls
    provider.sample_rows(..., row_count_estimate=...) unconditionally for
    every provider, but this provider's signature previously only accepted
    (schema, table, sample_size) — the same call from tasks.py would raise
    TypeError before this provider was ever reached (no connection, no
    query). This provider doesn't use the value (no size-aware sampling
    strategy implemented), but must accept it without error."""
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(1, "a")])
    cur.description = [("id",), ("val",)]

    result = provider.sample_rows("app_db", "orders", sample_size=20, row_count_estimate=5)

    assert result.rows == [{"id": 1, "val": "a"}]


def test_sample_rows_quotes_identifiers_containing_backticks() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    provider.sample_rows("weird`schema", "weird`table", sample_size=5)

    executed_sql = cur.execute.call_args[0][0]
    assert "`weird``schema`.`weird``table`" in executed_sql


def test_sample_rows_wraps_driver_error() -> None:
    import pytest
    import pymysql

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = pymysql.MySQLError("boom")

    with pytest.raises(SourceQueryError):
        provider.sample_rows("app_db", "orders", sample_size=20)

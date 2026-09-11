from unittest.mock import MagicMock

from app.source_adapters.saphana_provider import SAPHanaProvider, _normalize_type


def _make_provider() -> SAPHanaProvider:
    return SAPHanaProvider(host="localhost", port=30015, database="HDB", username="u", password="p")


def _wire_mock_connection(provider: SAPHanaProvider, fetchall_result=None, fetchone_result=None) -> MagicMock:
    cur = MagicMock()
    cur.fetchall.return_value = fetchall_result or []
    cur.fetchone.return_value = fetchone_result
    conn = MagicMock()
    conn.isconnected.return_value = True
    conn.cursor.return_value = cur
    provider._conn = conn
    return cur


def test_get_capabilities() -> None:
    caps = _make_provider().get_capabilities()
    assert caps.supports_foreign_keys is True
    assert caps.supports_exact_row_count is False


def test_normalize_type_mapping() -> None:
    assert _normalize_type("NVARCHAR") == "STRING"
    assert _normalize_type("NCLOB") == "TEXT"
    assert _normalize_type("INTEGER") == "INTEGER"
    assert _normalize_type("DECIMAL") == "DECIMAL"
    assert _normalize_type("SECONDDATE") == "DATETIME"
    assert _normalize_type("BOOLEAN") == "BOOLEAN"
    assert _normalize_type("SOMETHING_ELSE") == "STRING"


def test_list_schemas_excludes_sys_schemas() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("APP",)])

    assert provider.list_schemas() == ["APP"]


def test_list_datasets_merges_tables_and_views() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("USERS", "TABLE"), ("USER_SUMMARY", "VIEW")])

    datasets = provider.list_datasets("APP")

    assert datasets == [{"name": "USERS", "object_type": "TABLE"}, {"name": "USER_SUMMARY", "object_type": "VIEW"}]


def test_get_primary_keys_ordered_by_position() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("REGION_ID",), ("ORDER_ID",)])

    assert provider.get_primary_keys("APP", "ORDERS") == ["REGION_ID", "ORDER_ID"]


def test_get_row_count_from_m_tables() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchone_result=(777,))

    assert provider.get_row_count("APP", "ORDERS") == 777


def test_translate_error_invalid_credentials_maps_to_authentication() -> None:
    from app.source_adapters.exceptions import SourceAuthenticationError

    provider = _make_provider()
    try:
        provider._translate_error(Exception("invalid username or password"))
        assert False, "expected SourceAuthenticationError"
    except SourceAuthenticationError:
        pass


def test_sample_rows_returns_rows_as_dicts_with_limit_clause() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(1, "a"), (2, "b")])
    cur.description = [("ID",), ("VAL",)]

    result = provider.sample_rows("APP", "ORDERS", sample_size=20)

    assert result.rows == [{"ID": 1, "VAL": "a"}, {"ID": 2, "VAL": "b"}]
    assert result.is_full_scan is False
    executed_sql, params = cur.execute.call_args[0]
    assert "LIMIT ?" in executed_sql
    assert '"APP"."ORDERS"' in executed_sql
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
    cur.description = [("ID",), ("VAL",)]

    result = provider.sample_rows("APP", "ORDERS", sample_size=20, row_count_estimate=5)

    assert result.rows == [{"ID": 1, "VAL": "a"}]


def test_sample_rows_quotes_identifiers_containing_double_quotes() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    provider.sample_rows('weird"schema', 'weird"table', sample_size=5)

    executed_sql = cur.execute.call_args[0][0]
    assert '"weird""schema"."weird""table"' in executed_sql


def test_sample_rows_wraps_driver_error() -> None:
    from hdbcli import dbapi
    import pytest

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = dbapi.Error("boom")

    with pytest.raises(SourceQueryError):
        provider.sample_rows("APP", "ORDERS", sample_size=20)

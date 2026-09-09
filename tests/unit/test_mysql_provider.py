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

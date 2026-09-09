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

from unittest.mock import MagicMock

from app.source_adapters.oracle_provider import OracleProvider, _normalize_type


def _make_provider() -> OracleProvider:
    return OracleProvider(host="localhost", port=1521, database="ORCL", username="u", password="p")


def _wire_mock_connection(provider: OracleProvider, fetchall_result=None, fetchone_result=None) -> MagicMock:
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


def test_normalize_type_mapping_number_scale() -> None:
    assert _normalize_type("NUMBER", None) == "INTEGER"
    assert _normalize_type("NUMBER", 0) == "INTEGER"
    assert _normalize_type("NUMBER", 2) == "DECIMAL"
    assert _normalize_type("VARCHAR2", None) == "STRING"
    assert _normalize_type("CLOB", None) == "TEXT"
    assert _normalize_type("DATE", None) == "DATETIME"
    assert _normalize_type("TIMESTAMP(6)", None) == "DATETIME"
    assert _normalize_type("UNKNOWN_TYPE", None) == "STRING"


def test_list_schemas_excludes_system_owners() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("SYS",), ("APP_OWNER",)])

    assert provider.list_schemas() == ["APP_OWNER"]


def test_list_datasets_merges_tables_and_views() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.fetchall.side_effect = [[("USERS",)], [("USER_SUMMARY",)]]

    datasets = provider.list_datasets("APP_OWNER")

    assert datasets == [{"name": "USERS", "object_type": "TABLE"}, {"name": "USER_SUMMARY", "object_type": "VIEW"}]


def test_get_primary_keys_ordered_by_position() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchall_result=[("REGION_ID",), ("ORDER_ID",)])

    assert provider.get_primary_keys("APP_OWNER", "ORDERS") == ["REGION_ID", "ORDER_ID"]


def test_get_row_count_from_num_rows_stat() -> None:
    provider = _make_provider()
    _wire_mock_connection(provider, fetchone_result=(500,))

    assert provider.get_row_count("APP_OWNER", "ORDERS") == 500


def test_translate_error_ora_01017_maps_to_authentication() -> None:
    from app.source_adapters.exceptions import SourceAuthenticationError

    provider = _make_provider()
    try:
        provider._translate_error(Exception("ORA-01017: invalid username/password; logon denied"))
        assert False, "expected SourceAuthenticationError"
    except SourceAuthenticationError:
        pass


def test_translate_error_ora_12154_maps_to_unreachable() -> None:
    from app.source_adapters.exceptions import SourceUnreachableError

    provider = _make_provider()
    try:
        provider._translate_error(Exception("ORA-12154: TNS:could not resolve the connect identifier"))
        assert False, "expected SourceUnreachableError"
    except SourceUnreachableError:
        pass

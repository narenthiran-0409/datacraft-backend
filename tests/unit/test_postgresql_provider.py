from unittest.mock import MagicMock

from app.source_adapters.postgresql_provider import PostgreSQLProvider, _normalize_type


def _make_provider() -> PostgreSQLProvider:
    return PostgreSQLProvider(host="localhost", port=5432, database="db", username="u", password="p")


def _mock_cursor(fetchall_result=None, fetchone_result=None) -> MagicMock:
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchall.return_value = fetchall_result or []
    cur.fetchone.return_value = fetchone_result
    return cur


def test_get_capabilities() -> None:
    caps = _make_provider().get_capabilities()
    assert caps.supports_foreign_keys is True
    assert caps.supports_exact_row_count is False
    assert caps.supports_views is True


def test_normalize_type_mapping() -> None:
    assert _normalize_type("text") == "TEXT"
    assert _normalize_type("character varying") == "STRING"
    assert _normalize_type("integer") == "INTEGER"
    assert _normalize_type("numeric") == "DECIMAL"
    assert _normalize_type("date") == "DATE"
    assert _normalize_type("timestamp without time zone") == "DATETIME"
    assert _normalize_type("boolean") == "BOOLEAN"
    assert _normalize_type("some_unknown_type") == "STRING"


def test_list_schemas_excludes_system_schemas() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchall_result=[("public",), ("app",)])
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    schemas = provider.list_schemas()

    assert schemas == ["public", "app"]
    executed_sql = cur.execute.call_args[0][0]
    assert "pg_catalog" in executed_sql
    assert "information_schema" in executed_sql


def test_list_datasets_maps_table_type() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchall_result=[("users", "BASE TABLE"), ("user_summary", "VIEW")])
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    datasets = provider.list_datasets("public")

    assert datasets == [
        {"name": "users", "object_type": "TABLE"},
        {"name": "user_summary", "object_type": "VIEW"},
    ]


def test_get_columns_parses_rows() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchall_result=[("id", 1, "uuid", None, None, None, "NO"), ("email", 2, "character varying", 255, None, None, "NO")])
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    columns = provider.get_columns("public", "users")

    assert columns[0]["name"] == "id"
    assert columns[0]["is_nullable"] is False
    assert columns[1]["normalized_data_type"] == "STRING"
    assert columns[1]["max_length"] == 255


def test_get_primary_keys_returns_ordered_column_names() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchall_result=[("region_id",), ("order_id",)])
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    pks = provider.get_primary_keys("public", "orders")

    assert pks == ["region_id", "order_id"]


def test_get_row_count_returns_none_for_negative_reltuples() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchone_result=(-1.0,))
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    assert provider.get_row_count("public", "foreign_table") is None


def test_get_row_count_returns_estimate() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchone_result=(1234.0,))
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    assert provider.get_row_count("public", "users") == 1234


def test_get_foreign_keys_parses_rows() -> None:
    provider = _make_provider()
    cur = _mock_cursor(fetchall_result=[("data_source_id", "public", "data_sources", "id")])
    conn = MagicMock()
    conn.closed = False
    conn.cursor.return_value = cur
    provider._conn = conn

    fks = provider.get_foreign_keys("public", "connections")

    assert fks == [
        {
            "column": "data_source_id",
            "referenced_schema": "public",
            "referenced_table": "data_sources",
            "referenced_column": "id",
        }
    ]


# fetch_rows_by_keys() was implemented for real in Phase 8 (see
# tests/unit/test_postgresql_provider_staging.py for its real coverage) —
# the NotImplementedError placeholder test that used to live here is
# obsolete now that the deferred method has an actual implementation,
# exactly as Phase 8 was scoped to do.

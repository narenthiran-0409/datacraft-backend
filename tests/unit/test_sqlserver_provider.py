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
    # 2 rows back for a TOP (20) request proves the table only has 2 rows —
    # TOP is the only truncation mechanism this query uses, so fewer rows
    # than requested is conclusive, not a guess.
    assert result.is_full_scan is True
    executed_sql, param = cur.execute.call_args[0]
    assert "SELECT TOP (?)" in executed_sql
    assert "[dbo].[orders]" in executed_sql
    assert param == 20


def test_sample_rows_full_scan_true_when_zero_rows_returned_for_positive_request() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    result = provider.sample_rows("dbo", "orders", sample_size=5000)

    assert result.rows == []
    assert result.is_full_scan is True


def test_sample_rows_full_scan_true_when_returned_rows_well_under_limit() -> None:
    # Mirrors the real Customer_Orders shape: 15 real rows, TOP (5000) requested.
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(i,) for i in range(15)])
    cur.description = [("id",)]

    result = provider.sample_rows("dbo", "orders", sample_size=5000)

    assert len(result.rows) == 15
    assert result.is_full_scan is True


def test_sample_rows_full_scan_true_when_one_row_under_limit() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(i,) for i in range(4999)])
    cur.description = [("id",)]

    result = provider.sample_rows("dbo", "orders", sample_size=5000)

    assert len(result.rows) == 4999
    assert result.is_full_scan is True


def test_sample_rows_full_scan_false_when_returned_rows_exactly_equal_limit() -> None:
    # Ambiguous case: the table may have more rows than the TOP clause
    # returned — must stay conservative (False), never assume completeness.
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(i,) for i in range(5000)])
    cur.description = [("id",)]

    result = provider.sample_rows("dbo", "orders", sample_size=5000)

    assert len(result.rows) == 5000
    assert result.is_full_scan is False


def test_sample_rows_full_scan_false_when_source_has_more_rows_than_limit() -> None:
    # A source with >5000 real rows: TOP (5000) returns exactly 5000 —
    # indistinguishable at this layer from the exactly-5000-rows case above,
    # which is exactly why that case must stay False.
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(i,) for i in range(5000)])
    cur.description = [("id",)]

    result = provider.sample_rows("dbo", "orders", sample_size=5000)

    assert len(result.rows) == 5000
    assert result.is_full_scan is False


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


def test_fetch_rows_by_keys_empty_keys_returns_empty_without_querying() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    assert provider.fetch_rows_by_keys("dbo", "orders", []) == []
    cur.execute.assert_not_called()  # no scan fallback — never queries at all


def test_fetch_rows_by_keys_single_pk_exact_fetch() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(9, "suresh-invalid-email")])
    cur.description = [("order_id",), ("email",)]

    result = provider.fetch_rows_by_keys("dbo", "orders", [{"order_id": 9}])

    assert result == [{"order_id": 9, "email": "suresh-invalid-email"}]
    executed_sql, params = cur.execute.call_args[0]
    assert "[order_id] = ?" in executed_sql
    assert "[dbo].[orders]" in executed_sql
    assert params == [9]


def test_fetch_rows_by_keys_composite_pk_exact_fetch() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(5, 1002, "ok")])
    cur.description = [("region_id",), ("order_id",), ("status",)]

    result = provider.fetch_rows_by_keys("dbo", "orders", [{"region_id": 5, "order_id": 1002}])

    assert result == [{"region_id": 5, "order_id": 1002, "status": "ok"}]
    executed_sql, params = cur.execute.call_args[0]
    assert "[region_id] = ?" in executed_sql
    assert "[order_id] = ?" in executed_sql
    assert " AND " in executed_sql
    assert params == [5, 1002]


def test_fetch_rows_by_keys_no_match_returns_empty_list() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    assert provider.fetch_rows_by_keys("dbo", "orders", [{"order_id": 999}]) == []


def test_fetch_rows_by_keys_duplicate_result_is_returned_as_is_for_caller_to_judge() -> None:
    """The provider's job is exact retrieval, not judging key uniqueness —
    it must faithfully return every matching row (never silently drop or
    pick one); the caller (AISuggestionService) is what treats >1 row for
    one key as a safety failure, see suggestion_service.py."""
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[(9, "a"), (9, "b")])
    cur.description = [("order_id",), ("note",)]

    result = provider.fetch_rows_by_keys("dbo", "orders", [{"order_id": 9}])
    assert len(result) == 2


def test_fetch_rows_by_keys_uses_parameterized_placeholders_never_string_interpolation() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    # A value containing SQL-meaningful characters must travel as a bound
    # parameter, never concatenated into the query text.
    provider.fetch_rows_by_keys("dbo", "orders", [{"email": "a'; DROP TABLE orders; --"}])

    executed_sql, params = cur.execute.call_args[0]
    assert "DROP TABLE" not in executed_sql
    assert params == ["a'; DROP TABLE orders; --"]


def test_fetch_rows_by_keys_quotes_identifiers_containing_brackets() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    provider.fetch_rows_by_keys("weird]schema", "weird]table", [{"weird]col": 1}])

    executed_sql = cur.execute.call_args[0][0]
    assert "[weird]]schema].[weird]]table]" in executed_sql
    assert "[weird]]col]" in executed_sql


def test_fetch_rows_by_keys_rejects_empty_key_dict() -> None:
    import pytest

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    with pytest.raises(SourceQueryError):
        provider.fetch_rows_by_keys("dbo", "orders", [{}])


def test_fetch_rows_by_keys_rejects_inconsistent_key_column_sets() -> None:
    import pytest

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    with pytest.raises(SourceQueryError):
        provider.fetch_rows_by_keys("dbo", "orders", [{"order_id": 1}, {"customer_id": 2}])


def test_fetch_rows_by_keys_null_key_value_uses_is_null_not_equals() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[])
    cur.description = []

    provider.fetch_rows_by_keys("dbo", "orders", [{"middle_name": None}])

    executed_sql, params = cur.execute.call_args[0]
    assert "[middle_name] IS NULL" in executed_sql
    assert "[middle_name] = ?" not in executed_sql
    assert params == []  # IS NULL binds no parameter


def test_fetch_rows_by_keys_arbitrary_column_names() -> None:
    """Nothing about the query construction is specific to 'order_id',
    'PK', or any identifier-sounding name — any column name works."""
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchall_result=[("x1", 42)])
    cur.description = [("widget_code",), ("reading",)]

    result = provider.fetch_rows_by_keys("dbo", "sensors", [{"widget_code": "x1"}])
    assert result == [{"widget_code": "x1", "reading": 42}]


def test_fetch_rows_by_keys_wraps_driver_error() -> None:
    import pyodbc
    import pytest

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = pyodbc.Error("boom")

    with pytest.raises(SourceQueryError):
        provider.fetch_rows_by_keys("dbo", "orders", [{"order_id": 1}])


def test_sample_rows_wraps_driver_error() -> None:
    import pyodbc
    import pytest

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = pyodbc.Error("boom")

    with pytest.raises(SourceQueryError):
        provider.sample_rows("dbo", "orders", sample_size=20)


def test_count_rows_issues_count_star() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider, fetchone_result=(13,))

    assert provider.count_rows("dbo", "orders") == 13
    assert "COUNT(*)" in cur.execute.call_args[0][0]


def test_count_rows_wraps_driver_error() -> None:
    import pytest
    import pyodbc

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = pyodbc.Error("boom")

    with pytest.raises(SourceQueryError):
        provider.count_rows("dbo", "orders")


def test_iter_rows_yields_bounded_batches_using_offset_fetch_next() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.description = [("id",), ("name",)]
    cur.fetchall.side_effect = [[(1, "a"), (2, "b")], [(3, "c")]]

    batches = list(provider.iter_rows("dbo", "t", ["id", "name"], batch_size=2, order_by=["id"]))

    assert batches == [
        [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
        [{"id": 3, "name": "c"}],
    ]
    assert cur.execute.call_count == 2
    first_call_args = cur.execute.call_args_list[0][0]
    assert "OFFSET ? ROWS FETCH NEXT ? ROWS ONLY" in first_call_args[0]
    assert first_call_args[1:] == (0, 2)
    second_call_args = cur.execute.call_args_list[1][0]
    assert second_call_args[1:] == (2, 2)


def test_iter_rows_empty_table_yields_no_batches() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.description = [("id",)]
    cur.fetchall.side_effect = [[]]

    assert list(provider.iter_rows("dbo", "t", ["id"], batch_size=100)) == []


def test_iter_rows_falls_back_to_selected_columns_when_no_order_by() -> None:
    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.description = [("a",), ("b",)]
    cur.fetchall.side_effect = [[]]

    list(provider.iter_rows("dbo", "t", ["a", "b"], batch_size=10, order_by=None))

    executed_sql = cur.execute.call_args_list[0][0][0]
    assert "[a]" in executed_sql and "[b]" in executed_sql


def test_iter_rows_wraps_driver_error() -> None:
    import pytest
    import pyodbc

    from app.source_adapters.exceptions import SourceQueryError

    provider = _make_provider()
    cur = _wire_mock_connection(provider)
    cur.execute.side_effect = pyodbc.Error("boom")

    with pytest.raises(SourceQueryError):
        list(provider.iter_rows("dbo", "t", ["id"], batch_size=10))

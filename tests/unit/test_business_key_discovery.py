import inspect

from app.modules.datasets.business_key_discovery import (
    ColumnMeta,
    discover_business_key_candidates,
    STATUS_AMBIGUOUS,
    STATUS_NO_CANDIDATE,
    STATUS_SAMPLE_CANDIDATE,
    STATUS_VERIFIED_UNIQUE,
)


def _rows(*dicts):
    return list(dicts)


def test_unique_non_null_single_column_is_verified_when_full_scan():
    columns = [ColumnMeta("zzz_id", "INTEGER"), ColumnMeta("label", "STRING")]
    rows = _rows(
        {"zzz_id": 1, "label": "a"}, {"zzz_id": 2, "label": "b"}, {"zzz_id": 3, "label": "a"},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.columns == ("zzz_id",)
    assert result.recommended.width == 1


def test_duplicate_single_column_is_rejected():
    columns = [ColumnMeta("code", "INTEGER")]
    rows = _rows({"code": 1}, {"code": 2}, {"code": 1})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    rejected = [e for e in result.evaluated if e.columns == ("code",)][0]
    assert rejected.status == "REJECTED"
    assert rejected.reason == "duplicate_values"
    assert rejected.duplicate_key_groups == 1


def test_nullable_otherwise_unique_column_is_rejected():
    columns = [ColumnMeta("ref", "STRING")]
    rows = _rows({"ref": "a"}, {"ref": "b"}, {"ref": None})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    rejected = [e for e in result.evaluated if e.columns == ("ref",)][0]
    assert rejected.reason == "contains_null_values"
    assert rejected.null_key_rows == 1


def test_99_percent_unique_column_is_not_verified():
    # 99 rows unique + 1 duplicate of the first value: high distinct_percentage,
    # but exact uniqueness fails outright — must never be treated as "nearly a key".
    rows = [{"v": i} for i in range(1, 100)] + [{"v": 1}]
    columns = [ColumnMeta("v", "INTEGER")]
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    rejected = [e for e in result.evaluated if e.columns == ("v",)][0]
    assert rejected.status == "REJECTED"
    assert rejected.duplicate_key_groups == 1


def test_arbitrary_column_names_produce_same_result():
    columns = [ColumnMeta("xk9_qq", "INTEGER")]
    rows = _rows({"xk9_qq": 1}, {"xk9_qq": 2})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.columns == ("xk9_qq",)


def test_renamed_columns_produce_same_result():
    # Same shape as test_unique_non_null_single_column_is_verified_when_full_scan
    # but with every name replaced — the algorithm must not depend on naming.
    columns = [ColumnMeta("alpha", "INTEGER"), ColumnMeta("beta", "STRING")]
    rows = _rows(
        {"alpha": 1, "beta": "a"}, {"alpha": 2, "beta": "b"}, {"alpha": 3, "beta": "a"},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.columns == ("alpha",)


def test_minimal_two_column_composite_is_verified_when_no_single_column_works():
    columns = [ColumnMeta("region", "STRING"), ColumnMeta("seq", "INTEGER")]
    rows = _rows(
        {"region": "east", "seq": 1}, {"region": "east", "seq": 2},
        {"region": "west", "seq": 1}, {"region": "west", "seq": 2},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.columns == ("region", "seq")
    assert result.recommended.width == 2


def test_composite_not_recommended_when_a_proper_subset_is_already_unique():
    # "region" alone is already unique here, so (region, seq) must never be reached.
    columns = [ColumnMeta("region", "STRING"), ColumnMeta("seq", "INTEGER")]
    rows = _rows(
        {"region": "east", "seq": 1}, {"region": "west", "seq": 1}, {"region": "north", "seq": 2},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.columns == ("region",)
    assert result.widths_searched == (1,)


def test_three_column_combination_not_explored_when_two_column_already_wins():
    columns = [ColumnMeta("a", "STRING"), ColumnMeta("b", "STRING"), ColumnMeta("c", "STRING")]
    rows = _rows(
        {"a": "x", "b": "1", "c": "z"}, {"a": "x", "b": "2", "c": "z"},
        {"a": "y", "b": "1", "c": "z"}, {"a": "y", "b": "2", "c": "z"},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.columns == ("a", "b")
    assert result.widths_searched == (1, 2)


def test_free_text_column_excluded_from_key_construction():
    columns = [ColumnMeta("notes", "TEXT")]
    rows = _rows({"notes": "unique text 1"}, {"notes": "unique text 2"})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    assert result.rejected_columns[0].name == "notes"
    assert result.rejected_columns[0].reason == "unsupported_type"
    assert result.evaluated == ()


def test_unsuitable_blob_like_type_excluded():
    columns = [ColumnMeta("payload", "JSON"), ColumnMeta("attachment", "BLOB")]
    rows = _rows({"payload": "{}", "attachment": "x"}, {"payload": "{}", "attachment": "y"})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    assert {rc.name for rc in result.rejected_columns} == {"payload", "attachment"}


def test_floating_point_decimal_type_handled_conservatively_excluded():
    columns = [ColumnMeta("amount", "DECIMAL")]
    rows = _rows({"amount": "1.1"}, {"amount": "2.2"})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    assert result.rejected_columns[0].reason == "unsupported_type"


def test_sample_only_unique_candidate_is_not_verified():
    columns = [ColumnMeta("id", "INTEGER")]
    rows = _rows({"id": 1}, {"id": 2}, {"id": 3})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=False)
    assert result.status == STATUS_SAMPLE_CANDIDATE
    assert result.recommended.verification_level == STATUS_SAMPLE_CANDIDATE


def test_full_data_verified_candidate_is_adoptable_status():
    columns = [ColumnMeta("id", "INTEGER")]
    rows = _rows({"id": 1}, {"id": 2}, {"id": 3})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_VERIFIED_UNIQUE
    assert result.recommended.verification_level == STATUS_VERIFIED_UNIQUE


def test_multiple_equally_good_verified_candidates_are_ambiguous():
    columns = [ColumnMeta("a", "INTEGER"), ColumnMeta("b", "INTEGER")]
    rows = _rows({"a": 1, "b": 100}, {"a": 2, "b": 200}, {"a": 3, "b": 300})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_AMBIGUOUS
    assert result.recommended is None
    assert {c.columns for c in result.candidates} == {("a",), ("b",)}


def test_no_defensible_candidate_returns_no_candidate():
    columns = [ColumnMeta("a", "INTEGER"), ColumnMeta("b", "INTEGER")]
    rows = _rows(
        {"a": 1, "b": 1}, {"a": 1, "b": 1}, {"a": 2, "b": 2}, {"a": 2, "b": 2},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    assert result.recommended is None


def test_customer_orders_shaped_duplicate_is_naturally_rejected():
    """Generic reproduction of the real Customer_Orders shape (a
    duplicated id-like column alongside other columns) — using
    deliberately generic names to prove the algorithm needs no
    Customer_Orders-specific knowledge to reject the duplicate."""
    columns = [ColumnMeta("id_like_column", "INTEGER"), ColumnMeta("name_like_column", "STRING")]
    rows = _rows(
        {"id_like_column": 101, "name_like_column": "a"},
        {"id_like_column": 102, "name_like_column": "b"},
        {"id_like_column": 102, "name_like_column": "c"},  # duplicate, like real customer_id=1002
        {"id_like_column": 103, "name_like_column": "d"},
    )
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    rejected = [e for e in result.evaluated if e.columns == ("id_like_column",)][0]
    assert rejected.status == "REJECTED"
    assert rejected.reason == "duplicate_values"


def test_module_contains_no_hardcoded_business_names():
    import app.modules.datasets.business_key_discovery as module

    source = inspect.getsource(module).lower()
    forbidden = [
        "customer_id", "order_id", "invoice_no", "invoice_id", "customer_name",
        "customer_orders", "suresh", "sara", "email",
    ]
    for term in forbidden:
        assert term not in source, f"business_key_discovery.py must not hardcode {term!r}"


def test_insufficient_row_count_returns_no_candidate():
    columns = [ColumnMeta("id", "INTEGER")]
    result = discover_business_key_candidates(columns=columns, rows=[{"id": 1}], is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    assert result.reason == "insufficient_row_count"


def test_no_eligible_columns_returns_no_candidate():
    columns = [ColumnMeta("notes", "TEXT"), ColumnMeta("blob", "BLOB")]
    rows = _rows({"notes": "a", "blob": "x"}, {"notes": "b", "blob": "y"})
    result = discover_business_key_candidates(columns=columns, rows=rows, is_full_scan=True)
    assert result.status == STATUS_NO_CANDIDATE
    assert result.reason == "no_eligible_columns"


def test_detection_result_is_a_plain_value_object_no_mutation_hooks():
    """Detection must be incapable of mutating anything — it doesn't even
    receive a db session or dataset object, only plain data in and a
    frozen dataclass result out."""
    columns = [ColumnMeta("id", "INTEGER")]
    result = discover_business_key_candidates(columns=columns, rows=_rows({"id": 1}, {"id": 2}), is_full_scan=True)
    with __import__("pytest").raises(Exception):
        result.status = "MUTATED"

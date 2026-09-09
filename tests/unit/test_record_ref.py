from app.modules.validation.record_ref import compute_source_row_hash, generate_record_ref


def test_single_column_record_ref() -> None:
    ref = generate_record_ref(
        key_strategy="SINGLE_COLUMN", key_column_names_in_order=["id"], row={"id": "48213"}, row_index=0
    )
    assert ref == "48213"


def test_composite_record_ref_uses_unit_separator() -> None:
    ref = generate_record_ref(
        key_strategy="COMPOSITE",
        key_column_names_in_order=["region_id", "order_id"],
        row={"region_id": 5, "order_id": 1002},
        row_index=0,
    )
    assert ref == "5\x1f1002"


def test_composite_record_ref_null_uses_sentinel_not_empty_string() -> None:
    ref_null = generate_record_ref(
        key_strategy="COMPOSITE",
        key_column_names_in_order=["a", "b"],
        row={"a": 5, "b": None},
        row_index=0,
    )
    ref_empty = generate_record_ref(
        key_strategy="COMPOSITE",
        key_column_names_in_order=["a", "b"],
        row={"a": 5, "b": ""},
        row_index=0,
    )
    assert ref_null != ref_empty
    assert "NULL" in ref_null


def test_row_index_fallback_record_ref() -> None:
    ref = generate_record_ref(key_strategy="ROW_INDEX_FALLBACK", key_column_names_in_order=[], row={}, row_index=7)
    assert ref == "ROWIDX:7"


def test_source_row_hash_is_deterministic_and_order_stable() -> None:
    row = {"a": 1, "b": "x"}
    h1 = compute_source_row_hash(row=row, column_names_in_order=["a", "b"])
    h2 = compute_source_row_hash(row=row, column_names_in_order=["a", "b"])
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest


def test_source_row_hash_changes_when_value_changes() -> None:
    h1 = compute_source_row_hash(row={"a": 1}, column_names_in_order=["a"])
    h2 = compute_source_row_hash(row={"a": 2}, column_names_in_order=["a"])
    assert h1 != h2

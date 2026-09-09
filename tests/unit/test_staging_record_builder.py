import uuid
from decimal import Decimal

import pytest

from app.modules.staging.record_builder import (
    ScopeItem,
    StagingIntegrityViolationError,
    build_corrected_fields,
    build_row_snapshot,
    classify_drift,
    compute_staging_row_hash,
    group_by_record_ref,
    json_safe,
    parse_record_ref_to_key_dict,
)

ISSUE_A = uuid.uuid4()
ISSUE_B = uuid.uuid4()
COL_X = uuid.uuid4()
COL_Y = uuid.uuid4()


# --- group_by_record_ref -----------------------------------------------------

def test_multiple_issues_same_record_ref_produce_one_group() -> None:
    item_a = ScopeItem(issue_id=ISSUE_A, column_id=COL_X, original_value="old_a", correction_final_value="new_a")
    item_b = ScopeItem(issue_id=ISSUE_B, column_id=COL_Y, original_value="old_b", correction_final_value="new_b")
    groups = group_by_record_ref([("rec-1", item_a), ("rec-1", item_b), ("rec-2", item_a)])
    assert set(groups.keys()) == {"rec-1", "rec-2"}
    assert len(groups["rec-1"]) == 2
    assert len(groups["rec-2"]) == 1


# --- parse_record_ref_to_key_dict --------------------------------------------

def test_single_column_key_parses_directly() -> None:
    result = parse_record_ref_to_key_dict("48213", key_strategy="SINGLE_COLUMN", key_column_names=["id"])
    assert result == {"id": "48213"}


def test_composite_key_splits_on_unit_separator() -> None:
    result = parse_record_ref_to_key_dict(
        "5\x1f1002", key_strategy="COMPOSITE", key_column_names=["region_id", "order_id"]
    )
    assert result == {"region_id": "5", "order_id": "1002"}


def test_null_sentinel_becomes_none() -> None:
    result = parse_record_ref_to_key_dict(
        "5\x1f\x00NULL\x00", key_strategy="COMPOSITE", key_column_names=["a", "b"]
    )
    assert result == {"a": "5", "b": None}


def test_row_index_fallback_returns_none_unfetchable() -> None:
    assert parse_record_ref_to_key_dict("ROWIDX:3", key_strategy="ROW_INDEX_FALLBACK", key_column_names=[]) is None


def test_no_key_columns_returns_none_even_if_strategy_claims_otherwise() -> None:
    assert parse_record_ref_to_key_dict("48213", key_strategy="SINGLE_COLUMN", key_column_names=[]) is None


# --- json_safe -----------------------------------------------------------

def test_json_safe_converts_decimal_to_float() -> None:
    assert json_safe(Decimal("10.5")) == 10.5


def test_json_safe_converts_datetime_to_isoformat() -> None:
    import datetime

    dt = datetime.datetime(2026, 1, 1, 12, 0, 0)
    assert json_safe(dt) == dt.isoformat()


def test_json_safe_preserves_none_as_genuine_null() -> None:
    assert json_safe(None) is None


def test_json_safe_passes_through_plain_values() -> None:
    assert json_safe("abc") == "abc"
    assert json_safe(5) == 5


# --- build_corrected_fields -----------------------------------------------------

def test_build_corrected_fields_produces_one_entry_per_item() -> None:
    items = [
        ScopeItem(issue_id=ISSUE_A, column_id=COL_X, original_value="old_a", correction_final_value="new_a"),
        ScopeItem(issue_id=ISSUE_B, column_id=COL_Y, original_value="old_b", correction_final_value="new_b"),
    ]
    fields = build_corrected_fields(items, {COL_X: "col_x", COL_Y: "col_y"})
    assert len(fields) == 2
    assert fields[0] == {"column_name": "col_x", "original_value": "old_a", "final_value": "new_a", "issue_id": str(ISSUE_A)}


def test_build_corrected_fields_raises_on_missing_correction() -> None:
    items = [ScopeItem(issue_id=ISSUE_A, column_id=COL_X, original_value="old_a", correction_final_value=None)]
    with pytest.raises(StagingIntegrityViolationError):
        build_corrected_fields(items, {COL_X: "col_x"})


# --- build_row_snapshot -----------------------------------------------------

def test_row_snapshot_overlays_corrected_fields_on_fetched_row() -> None:
    fetched_row = {"id": 1, "val": "old_a", "other": "unchanged"}
    corrected_fields = [{"column_name": "val", "original_value": "old_a", "final_value": "new_a", "issue_id": "x"}]
    snapshot = build_row_snapshot(fetched_row, corrected_fields)
    assert snapshot == {"id": 1, "val": "new_a", "other": "unchanged"}


def test_row_snapshot_degrades_to_corrected_fields_only_when_record_not_found() -> None:
    corrected_fields = [{"column_name": "val", "original_value": "old_a", "final_value": "new_a", "issue_id": "x"}]
    snapshot = build_row_snapshot(None, corrected_fields)
    assert snapshot == {"val": "new_a"}


def test_row_snapshot_null_values_are_genuine_none_not_string() -> None:
    snapshot = build_row_snapshot({"id": 1, "val": None}, [])
    assert snapshot["val"] is None
    assert snapshot["val"] != "null"


# --- compute_staging_row_hash -----------------------------------------------------

def test_staging_row_hash_is_deterministic() -> None:
    row = {"id": 1, "val": "a"}
    assert compute_staging_row_hash(row) == compute_staging_row_hash(row)


def test_staging_row_hash_independent_of_dict_key_insertion_order() -> None:
    row_a = {"id": 1, "val": "a"}
    row_b = {"val": "a", "id": 1}
    assert compute_staging_row_hash(row_a) == compute_staging_row_hash(row_b)


def test_staging_row_hash_changes_when_value_changes() -> None:
    assert compute_staging_row_hash({"id": 1}) != compute_staging_row_hash({"id": 2})


def test_staging_row_hash_handles_decimal_and_datetime() -> None:
    import datetime

    row = {"amount": Decimal("10.50"), "ts": datetime.datetime(2026, 1, 1)}
    # Should not raise, and should be deterministic.
    assert compute_staging_row_hash(row) == compute_staging_row_hash(row)


# --- classify_drift (corrected-field-level, NOT hash-based) -----------------------

def test_classify_drift_record_not_found() -> None:
    corrected_fields = [{"column_name": "val", "original_value": "a", "final_value": "b", "issue_id": "x"}]
    status, fields = classify_drift(fetched_row_found=False, corrected_fields=corrected_fields, fetched_row=None)
    assert status == "RECORD_NOT_FOUND"
    assert fields is None


def test_classify_drift_unchanged_when_corrected_field_value_matches() -> None:
    corrected_fields = [{"column_name": "val", "original_value": "old_a", "final_value": "new_a", "issue_id": "x"}]
    fetched_row = {"val": "old_a"}  # source still shows the pre-correction value — unchanged since validation
    status, fields = classify_drift(fetched_row_found=True, corrected_fields=corrected_fields, fetched_row=fetched_row)
    assert status == "UNCHANGED"
    assert fields is None


def test_classify_drift_value_changed_when_corrected_field_value_differs() -> None:
    corrected_fields = [{"column_name": "val", "original_value": "old_a", "final_value": "new_a", "issue_id": "x"}]
    fetched_row = {"val": "changed_externally"}
    status, fields = classify_drift(fetched_row_found=True, corrected_fields=corrected_fields, fetched_row=fetched_row)
    assert status == "VALUE_CHANGED"
    assert fields == ["val"]


def test_classify_drift_only_names_the_differing_corrected_columns() -> None:
    corrected_fields = [
        {"column_name": "a", "original_value": "old_a", "final_value": "new_a", "issue_id": "x"},
        {"column_name": "b", "original_value": "old_b", "final_value": "new_b", "issue_id": "y"},
    ]
    fetched_row = {"a": "changed_externally", "b": "old_b"}  # only 'a' drifted
    status, fields = classify_drift(fetched_row_found=True, corrected_fields=corrected_fields, fetched_row=fetched_row)
    assert status == "VALUE_CHANGED"
    assert fields == ["a"]


def test_classify_drift_never_flags_uncorrected_columns() -> None:
    """The accepted limitation (locked decision 9): an uncorrected column
    changing is never detected, because classify_drift only ever looks at
    corrected_fields entries — it has no visibility into any other column."""
    corrected_fields = [{"column_name": "a", "original_value": "old_a", "final_value": "new_a", "issue_id": "x"}]
    # 'b' is not a corrected field at all, even though its fetched value
    # differs from whatever it may have been — classify_drift never even
    # looks at it.
    fetched_row = {"a": "old_a", "b": "some_new_uncorrected_value"}
    status, fields = classify_drift(fetched_row_found=True, corrected_fields=corrected_fields, fetched_row=fetched_row)
    assert status == "UNCHANGED"
    assert fields is None

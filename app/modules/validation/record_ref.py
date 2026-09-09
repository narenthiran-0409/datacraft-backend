"""record_ref and source_row_hash generation, per the frozen Database Design
v2's explicit "Record Reference (record_ref) Generation" specification
(Section 2). Used by the Validation Service/engine at validation time — a
future Staging Service must recompute record_ref identically when
re-sampling, per that same spec, but that is out of scope for Phase 5.
"""
import hashlib
from typing import Any

_NULL_SENTINEL = "\x00NULL\x00"
_UNIT_SEPARATOR = "\x1f"


def generate_record_ref(
    *, key_strategy: str, key_column_names_in_order: list[str], row: dict[str, Any], row_index: int
) -> str:
    if key_strategy == "SINGLE_COLUMN":
        value = row.get(key_column_names_in_order[0])
        return str(value) if value is not None else _NULL_SENTINEL

    if key_strategy == "COMPOSITE":
        parts = [
            str(row.get(name)) if row.get(name) is not None else _NULL_SENTINEL
            for name in key_column_names_in_order
        ]
        return _UNIT_SEPARATOR.join(parts)

    # ROW_INDEX_FALLBACK
    return f"ROWIDX:{row_index}"


def compute_source_row_hash(*, row: dict[str, Any], column_names_in_order: list[str]) -> str:
    """SHA-256 over a canonical (stable column order, normalized value
    serialization) representation of every source column's value for this
    row — the "fingerprint as of validation time" a future Staging phase
    will compare against (validation_results.source_row_hash)."""
    parts = [
        str(row.get(name)) if row.get(name) is not None else _NULL_SENTINEL for name in column_names_in_order
    ]
    canonical = _UNIT_SEPARATOR.join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

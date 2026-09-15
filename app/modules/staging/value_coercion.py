"""Coerces an approved correction's final_value (persisted as a generic
TEXT string on `corrections.final_value`) into a Python value safe to bind
into a normalized-type-mapped physical staging column, for Phase 4.12's
APPLYING_CORRECTIONS phase. Pure, no DB access.

Never relies on PostgreSQL's implicit text->column casts: every value is
explicitly converted here first, and any value that cannot be safely
converted raises CorrectionCoercionError — the caller (the materialization
task) treats that as a hard failure of the whole run, never a silently
skipped/truncated write and never a fallback to writing the raw string into
a non-text column.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_TRUE_STRINGS = {"true", "t", "1", "yes", "y"}
_FALSE_STRINGS = {"false", "f", "0", "no", "n"}


class CorrectionCoercionError(Exception):
    """Raised when an approved correction's final_value cannot be safely
    coerced to its destination column's normalized data type. Caught by the
    materialization task and converted into a FAILED run — never silently
    ignored, never written as malformed text into a typed column."""


def coerce_final_value(raw_value: str | None, *, normalized_type: str, column_name: str) -> Any:
    """raw_value is None only for a legitimately-approved NULL — passed
    through as a genuine NULL regardless of destination type. Every other
    value is converted according to normalized_type; STRING/TEXT and any
    unrecognized normalized type pass through as-is (already a string)."""
    if raw_value is None:
        return None

    if normalized_type == "INTEGER":
        try:
            return int(raw_value.strip())
        except (ValueError, AttributeError) as exc:
            raise CorrectionCoercionError(
                f"Cannot coerce correction value {raw_value!r} for column '{column_name}' to INTEGER"
            ) from exc

    if normalized_type == "DECIMAL":
        try:
            return Decimal(raw_value.strip())
        except (InvalidOperation, AttributeError) as exc:
            raise CorrectionCoercionError(
                f"Cannot coerce correction value {raw_value!r} for column '{column_name}' to DECIMAL"
            ) from exc

    if normalized_type == "BOOLEAN":
        lowered = raw_value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
        raise CorrectionCoercionError(
            f"Cannot coerce correction value {raw_value!r} for column '{column_name}' to BOOLEAN"
        )

    if normalized_type == "DATE":
        try:
            return date.fromisoformat(raw_value.strip()[:10])
        except (ValueError, AttributeError) as exc:
            raise CorrectionCoercionError(
                f"Cannot coerce correction value {raw_value!r} for column '{column_name}' to DATE"
            ) from exc

    if normalized_type == "DATETIME":
        stripped = raw_value.strip()
        try:
            return datetime.fromisoformat(stripped)
        except ValueError:
            pass
        try:
            return datetime.combine(date.fromisoformat(stripped[:10]), datetime.min.time())
        except (ValueError, AttributeError) as exc:
            raise CorrectionCoercionError(
                f"Cannot coerce correction value {raw_value!r} for column '{column_name}' to DATETIME"
            ) from exc

    # STRING, TEXT, and any unrecognized normalized type — already a string.
    return raw_value

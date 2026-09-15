"""Normalized data type -> PostgreSQL staging column DDL type, for Phase
4.12's physical materialized staging tables. Pure, no DB/provider access.

Covers exactly the normalized types this repository's providers currently
emit (see each source_adapters/*_provider.py's own _TYPE_MAP): STRING, TEXT,
INTEGER, DECIMAL, DATE, DATETIME, BOOLEAN. Any other/unrecognized value
(including a future normalized type this mapping hasn't been taught yet)
falls back to TEXT — a lossless, conservative staging representation rather
than inventing vendor DDL or guessing.

INTEGER maps to BIGINT rather than INTEGER/SMALLINT: normalized_data_type
does not distinguish source int width (SQL Server's tinyint/smallint/int/
bigint all collapse to "INTEGER", see sqlserver_provider._TYPE_MAP), so the
widest safe PostgreSQL integer type is used unconditionally rather than
risking an overflow on a column that happened to be a smaller native type.
"""
from __future__ import annotations

_MAX_NUMERIC_PRECISION = 1000  # PostgreSQL's own NUMERIC precision ceiling
_MAX_VARCHAR_LENGTH = 10_485_760  # PostgreSQL's own character-length ceiling


def normalized_type_to_pg_ddl(
    normalized_type: str,
    *,
    max_length: int | None = None,
    numeric_precision: int | None = None,
    numeric_scale: int | None = None,
) -> str:
    """Returns a PostgreSQL column type DDL fragment (e.g. "NUMERIC(10,2)",
    "VARCHAR(255)", "TIMESTAMP") for the given normalized type and, where
    reliably available and safe, its precision/scale/length metadata.

    Falls back to an unbounded/conservative type whenever the metadata is
    missing or looks unsafe to reproduce exactly (e.g. an out-of-range
    precision) — never raises, since a materialized staging column must
    always be creatable."""
    if normalized_type == "INTEGER":
        return "BIGINT"

    if normalized_type == "DECIMAL":
        if (
            numeric_precision is not None
            and numeric_scale is not None
            and 1 <= numeric_precision <= _MAX_NUMERIC_PRECISION
            and 0 <= numeric_scale <= numeric_precision
        ):
            return f"NUMERIC({numeric_precision},{numeric_scale})"
        return "NUMERIC"

    if normalized_type == "BOOLEAN":
        return "BOOLEAN"

    if normalized_type == "DATE":
        return "DATE"

    if normalized_type == "DATETIME":
        return "TIMESTAMP"

    if normalized_type == "TEXT":
        return "TEXT"

    if normalized_type == "STRING":
        if max_length is not None and 1 <= max_length <= _MAX_VARCHAR_LENGTH:
            return f"VARCHAR({max_length})"
        return "TEXT"

    # Unrecognized normalized type — lossless conservative fallback, never
    # vendor-specific DDL, never a guess.
    return "TEXT"

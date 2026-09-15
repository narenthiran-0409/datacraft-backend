"""Server-controlled destination schema/table/identifier naming for Phase
4.12's materialized staging tables. Pure, no DB access.

The frontend never supplies or influences a destination identifier —
build_destination_table_name is the ONLY place a physical table name is
constructed, always from a dataset name (sanitized) plus the owning
staging_run's own id (already a globally-unique, server-generated UUID),
never from anything else in the request. quote_pg_identifier is the only
sanitization path used when composing DDL/DML against staging_data.* —
every value is still bound as a query parameter; only identifiers ever go
through this function.
"""
from __future__ import annotations

import re
import uuid

STAGING_SCHEMA_NAME = "staging_data"

# PostgreSQL's own unquoted-identifier length ceiling (NAMEDATALEN=64, i.e.
# 63 usable bytes) — applies here too since destination_table is still a
# real identifier, just always emitted quoted for safety.
_MAX_IDENTIFIER_LENGTH = 63

_INVALID_CHARS = re.compile(r"[^a-z0-9_]")
_REPEATED_UNDERSCORES = re.compile(r"_+")

_RUN_SUFFIX_LENGTH = 12
_SEPARATOR = "__"


def sanitize_identifier_part(raw: str) -> str:
    """Lowercases, replaces any run of non [a-z0-9_] characters with a
    single underscore, strips leading/trailing underscores, and guarantees
    a non-empty, non-digit-leading result — safe to use as (part of) a
    PostgreSQL identifier even before quoting."""
    lowered = raw.strip().lower()
    cleaned = _INVALID_CHARS.sub("_", lowered)
    cleaned = _REPEATED_UNDERSCORES.sub("_", cleaned).strip("_")
    if not cleaned:
        cleaned = "dataset"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


def build_destination_table_name(dataset_name: str, staging_run_id: uuid.UUID) -> str:
    """Deterministic per-call (same inputs -> same name), collision-safe
    across concurrent/repeated staging runs (the suffix is derived from
    staging_run_id, which is a fresh UUID per run — never reused), audit-
    friendly (the sanitized dataset name stays a visible prefix), and always
    within PostgreSQL's unquoted-identifier length ceiling.

    Deliberately keyed on staging_run_id rather than only the dataset name:
    multiple staging runs for the same dataset must be able to coexist as
    separate physical tables (retries, re-staging after a new approval
    cycle) — see the Phase 4.12 retry/idempotency requirement."""
    suffix = staging_run_id.hex[:_RUN_SUFFIX_LENGTH]
    sanitized = sanitize_identifier_part(dataset_name)

    max_name_length = _MAX_IDENTIFIER_LENGTH - len(_SEPARATOR) - len(suffix)
    if len(sanitized) > max_name_length:
        sanitized = sanitized.rstrip("_")[:max_name_length].rstrip("_") or "dataset"

    return f"{sanitized}{_SEPARATOR}{suffix}"


def quote_pg_identifier(name: str) -> str:
    """Double-quotes a PostgreSQL identifier, doubling any internal `"` —
    the standard escaping rule. Used only for server-generated identifiers
    (schema/table/column names already produced by this module or read from
    `columns.name` metadata) — actual VALUES are always bound as query
    parameters, never interpolated through this helper."""
    return '"' + name.replace('"', '""') + '"'

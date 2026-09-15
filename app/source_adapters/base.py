from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator


class ConnectionTestResult:
    def __init__(self, status: str, latency_ms: int | None = None, message: str | None = None) -> None:
        self.status = status
        self.latency_ms = latency_ms
        self.message = message


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_foreign_keys: bool
    supports_exact_row_count: bool
    supports_views: bool
    supports_native_sampling: bool = False


@dataclass(frozen=True)
class ColumnExactStats:
    null_count: int
    distinct_count: int
    total_row_count: int


@dataclass(frozen=True)
class SampleResult:
    rows: list[dict[str, Any]]
    is_full_scan: bool


class SourceDatabaseProvider(ABC):
    """Abstraction over a target source database engine.

    Phase 2 fully implemented test_connection() for PostgreSQL only,
    with sample_rows/fetch_rows_by_keys left for a later phase.

    Phase 3 adds real schema-discovery methods for all five providers
    (PostgreSQL, SQL Server, MySQL, Oracle, SAP HANA): get_capabilities,
    list_schemas, list_datasets (renamed from list_tables), get_columns
    (renamed from list_columns), get_primary_keys, get_foreign_keys,
    get_row_count. sample_rows/fetch_rows_by_keys remain out of scope.

    Phase 4 adds get_dataset_column_stats() (exact push-down null/distinct
    counts, batched and bounded-timeout) and changes sample_rows()'s
    signature: the offset parameter from the Phase 2/3 stub is removed
    (each profiling run pulls exactly one bounded sample, no pagination
    requirement this phase) and it now returns a SampleResult rather than a
    bare list. fetch_rows_by_keys() remains NotImplementedError, deferred
    to Staging.

    Phase 8 implements fetch_rows_by_keys() for PostgreSQL — a single
    batched query per call (Postgres row-value IN syntax), never one query
    per record. The other four providers remain NotImplementedError,
    consistent with this project's standing PostgreSQL-only live-
    verification policy (mock-only for SQL Server/MySQL/Oracle/SAP HANA).

    Phase 4.12 adds count_rows() (an exact, bounded-timeout row count — NOT
    the cheap catalog-statistics estimate get_row_count() already provides)
    and iter_rows() (a bounded-memory, batched full-table read), implemented
    for all five providers — the provider-agnostic full-read contract
    materialized staging is built on. Neither method ever writes; both are
    read-only, exactly like every other method on this interface.
    """

    @abstractmethod
    def test_connection(self) -> ConnectionTestResult:
        """Bounded-timeout connectivity check. Never raises for a normal
        connectivity failure — translates driver errors into the
        source_adapters exception taxonomy, which callers turn into a
        FAILED test result, not an HTTP error."""

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities:
        """Static, no-network-call description of what this provider can
        report. Discovery uses this to decide whether to call
        get_foreign_keys() at all and whether row counts are exact.
        Profiling uses supports_native_sampling to decide whether
        sample_rows() can use a server-side sampling clause or must fall
        back to a plain LIMIT."""

    @abstractmethod
    def list_schemas(self) -> list[str]:
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def list_datasets(self, schema: str) -> list[dict[str, Any]]:
        """Returns [{"name": ..., "object_type": "TABLE"|"VIEW"}, ...]."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def get_columns(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        """Returns column metadata in source-reported ordinal order."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def get_primary_keys(self, schema: str, dataset: str) -> list[str]:
        """Returns primary-key column names in the source's reported key
        order. The single source of truth for BOTH single-column and
        composite primary key detection — 0 columns means no PK, 1 means
        single-column, 2+ means composite. Callers must not special-case
        single vs. composite; discovery. treats this list generically."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def get_foreign_keys(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        """Returns [] if the provider's capabilities report
        supports_foreign_keys=False, rather than raising."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def get_row_count(self, schema: str, dataset: str) -> int | None:
        """A cheap catalog-statistics estimate only — never a live
        COUNT(*). Returns None if the provider can't cheaply estimate."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def get_dataset_column_stats(
        self, schema: str, table: str, columns: list[str]
    ) -> dict[str, ColumnExactStats]:
        """Exact (not estimated) null_count/distinct_count/total_row_count
        per requested column, via a single aggregate push-down query,
        batched into multiple queries if len(columns) exceeds
        PROFILING_EXACT_STATS_MAX_COLUMNS_PER_QUERY. Bounded by
        PROFILING_EXACT_STATS_TIMEOUT_SECONDS — raises
        ExactStatsTimeoutError (not a raw driver exception) if exceeded.
        Callers must treat a raised ExactStatsTimeoutError as "no exact
        stats available this run", not a fatal error."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def sample_rows(
        self, schema: str, table: str, sample_size: int, row_count_estimate: int | None = None
    ) -> SampleResult:
        """Pulls one bounded sample of up to sample_size rows. No offset —
        each profiling run pulls exactly one sample. SampleResult.is_full_scan
        is True only when every row of the table was returned (sample_size
        >= the table's actual row count for this call).

        row_count_estimate is an optional hint (datasets.row_count_estimate)
        a provider MAY use to pick a cheaper/more accurate sampling strategy
        (PostgreSQLProvider uses it to choose between a plain full-table
        SELECT, TABLESAMPLE, and LIMIT). Every provider must accept it —
        callers pass it unconditionally — but a provider without a
        size-aware strategy may simply ignore it."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def fetch_rows_by_keys(self, schema: str, table: str, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def count_rows(self, schema: str, table: str) -> int:
        """An EXACT row count via a live SELECT COUNT(*) — deliberately
        distinct from get_row_count()'s cheap catalog-statistics estimate.
        Used only by staging materialization to establish the
        source_row_count invariant a completed materialization run must
        match. Bounded by STAGING_MATERIALIZATION_QUERY_TIMEOUT_SECONDS."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def iter_rows(
        self, schema: str, table: str, columns: list[str], batch_size: int, order_by: list[str] | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        """Bounded-memory, batched full-table read for staging
        materialization. Yields lists of up to batch_size row-dicts
        (selecting exactly `columns`, in that order) until the table is
        exhausted — never loads the whole table into memory at once (unlike
        sample_rows(), which returns one fully-materialized SampleResult).
        Never writes.

        order_by, when given (the dataset's resolved business-key column
        names), is used as a deterministic ORDER BY for stable pagination.
        When None (a ROW_INDEX_FALLBACK dataset with no configured key),
        `columns` itself is used as the ORDER BY instead, so pagination is
        still deterministic (every row appears in exactly one batch)
        despite there being no natural key.

        Implemented via ORDER BY + OFFSET/LIMIT (or each vendor's
        equivalent) rather than a server-side cursor — correct for a stable
        snapshot of the source table, but callers should be aware this does
        not guard against the source table's row count changing mid-copy
        (a concurrent insert/delete could shift which rows later OFFSET
        windows return). The one invariant this method's caller CAN and
        does check is the total row count via count_rows() taken
        immediately before iteration begins, compared against the number of
        rows actually yielded.

        Callers may abandon a partially-consumed iterator (e.g. on
        cancellation) without further cleanup beyond the provider's own
        close() — no server-side cursor is held open across yields, only
        the ordinary cached connection every other method on this provider
        already reuses."""
        raise NotImplementedError("Implemented in a later phase")

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError("Implemented in a later phase")

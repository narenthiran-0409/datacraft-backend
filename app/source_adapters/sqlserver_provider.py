"""SQL Server provider via pyodbc.

Only mock-tested in this environment — there is no live SQL Server
instance available here. See tests/unit/test_sqlserver_provider.py.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

from app.core.config import settings
from app.source_adapters.base import ColumnExactStats, ConnectionTestResult, ProviderCapabilities, SampleResult, SourceDatabaseProvider
from app.source_adapters.exceptions import (
    SourceAuthenticationError,
    SourceDriverNotInstalledError,
    SourceQueryError,
    SourceSSLError,
    SourceTimeoutError,
    SourceUnreachableError,
)
from app.source_adapters.timeout import with_timeout

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_ODBC_DRIVER = "{ODBC Driver 17 for SQL Server}"

# Populated by _import_pyodbc() on first use — see postgresql_provider.py
# for why this driver import is deferred rather than a top-level import.
# Note: pyodbc also requires the unixODBC system library to be installed
# (on Linux) for the import itself to succeed; that failure surfaces here
# too, as ImportError, so it is covered by the same message.
pyodbc = None


def _import_pyodbc() -> None:
    global pyodbc
    if pyodbc is not None:
        return
    try:
        import pyodbc as _pyodbc
    except ImportError as exc:
        raise SourceDriverNotInstalledError(
            "SQL Server provider requires the 'pyodbc' package (and its unixODBC system "
            "dependency), which is not installed."
        ) from exc
    pyodbc = _pyodbc

_TYPE_MAP = {
    "varchar": "STRING",
    "nvarchar": "STRING",
    "char": "STRING",
    "nchar": "STRING",
    "text": "TEXT",
    "ntext": "TEXT",
    "int": "INTEGER",
    "bigint": "INTEGER",
    "smallint": "INTEGER",
    "tinyint": "INTEGER",
    "decimal": "DECIMAL",
    "numeric": "DECIMAL",
    "float": "DECIMAL",
    "real": "DECIMAL",
    "money": "DECIMAL",
    "smallmoney": "DECIMAL",
    "date": "DATE",
    "datetime": "DATETIME",
    "datetime2": "DATETIME",
    "smalldatetime": "DATETIME",
    "datetimeoffset": "DATETIME",
    "bit": "BOOLEAN",
}


def _normalize_type(native_type: str) -> str:
    return _TYPE_MAP.get(native_type.lower(), "STRING")


def _quote_ident(name: str) -> str:
    """Bracket-quotes a SQL Server identifier, doubling any internal ']' —
    the standard T-SQL escaping rule for bracketed identifiers."""
    return "[" + name.replace("]", "]]") + "]"


def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


class SQLServerProvider(SourceDatabaseProvider):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str | None,
        username: str,
        password: str,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        odbc_driver: str = DEFAULT_ODBC_DRIVER,
        **_kwargs: Any,
    ) -> None:
        _import_pyodbc()
        self._host = host
        self._port = port
        self._database = database
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._odbc_driver = odbc_driver
        self._conn: pyodbc.Connection | None = None

    def _connection_string(self) -> str:
        return (
            f"DRIVER={self._odbc_driver};SERVER={self._host},{self._port};"
            f"DATABASE={self._database or 'master'};UID={self._username};PWD={self._password};"
            "TrustServerCertificate=yes;"
        )

    def _connect(self) -> pyodbc.Connection:
        try:
            return pyodbc.connect(self._connection_string(), timeout=self._connect_timeout)
        except pyodbc.Error as exc:
            self._translate_error(exc)

    def _connection(self) -> pyodbc.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _translate_error(self, exc: Exception) -> None:
        message = str(exc).lower()

        if "login failed" in message or "authentication" in message:
            raise SourceAuthenticationError(f"Authentication failed for user '{self._username}'") from exc
        if "timeout" in message or "timed out" in message:
            raise SourceTimeoutError(
                f"Connection to {self._host}:{self._port} timed out after {self._connect_timeout}s"
            ) from exc
        if "ssl" in message or "tls" in message or "certificate" in message:
            raise SourceSSLError(f"SSL/TLS negotiation failed with {self._host}:{self._port}") from exc
        if (
            "could not open a connection" in message
            or "server was not found" in message
            or "connection refused" in message
            or "network-related" in message
        ):
            raise SourceUnreachableError(f"Could not reach {self._host}:{self._port}") from exc

        raise SourceQueryError(str(exc)) from exc

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            conn = pyodbc.connect(self._connection_string(), timeout=self._connect_timeout)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            finally:
                conn.close()
        except pyodbc.Error as exc:
            self._translate_error(exc)
        else:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ConnectionTestResult(status="HEALTHY", latency_ms=latency_ms, message="Connection successful")

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_foreign_keys=True, supports_exact_row_count=False, supports_views=True)

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_schemas(self) -> list[str]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA
                WHERE SCHEMA_NAME NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest',
                    'db_owner', 'db_accessadmin', 'db_securityadmin', 'db_ddladmin',
                    'db_backupoperator', 'db_datareader', 'db_datawriter',
                    'db_denydatareader', 'db_denydatawriter')
                ORDER BY SCHEMA_NAME
                """
            )
            return [row[0] for row in cur.fetchall()]
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_datasets(self, schema: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_TYPE FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = ? AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                ORDER BY TABLE_NAME
                """,
                schema,
            )
            return [
                {"name": name, "object_type": "TABLE" if table_type == "BASE TABLE" else "VIEW"}
                for name, table_type in cur.fetchall()
            ]
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_columns(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                       NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                ORDER BY ORDINAL_POSITION
                """,
                schema,
                dataset,
            )
            return [
                {
                    "name": name,
                    "ordinal_position": ordinal_position,
                    "native_data_type": data_type,
                    "normalized_data_type": _normalize_type(data_type),
                    "max_length": max_length,
                    "numeric_precision": numeric_precision,
                    "numeric_scale": numeric_scale,
                    "is_nullable": is_nullable == "YES",
                }
                for name, ordinal_position, data_type, max_length, numeric_precision, numeric_scale, is_nullable
                in cur.fetchall()
            ]
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_primary_keys(self, schema: str, dataset: str) -> list[str]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT kcu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY' AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
                ORDER BY kcu.ORDINAL_POSITION
                """,
                schema,
                dataset,
            )
            return [row[0] for row in cur.fetchall()]
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_foreign_keys(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT kcu.COLUMN_NAME, ccu.TABLE_SCHEMA, ccu.TABLE_NAME, ccu.COLUMN_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                  ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
                JOIN INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE ccu
                  ON tc.CONSTRAINT_NAME = ccu.CONSTRAINT_NAME
                WHERE tc.CONSTRAINT_TYPE = 'FOREIGN KEY' AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
                """,
                schema,
                dataset,
            )
            return [
                {
                    "column": column_name,
                    "referenced_schema": ref_schema,
                    "referenced_table": ref_table,
                    "referenced_column": ref_column,
                }
                for column_name, ref_schema, ref_table, ref_column in cur.fetchall()
            ]
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_row_count(self, schema: str, dataset: str) -> int | None:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT SUM(p.rows)
                FROM sys.tables t
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                JOIN sys.partitions p ON t.object_id = p.object_id AND p.index_id IN (0, 1)
                WHERE s.name = ? AND t.name = ?
                """,
                schema,
                dataset,
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def get_dataset_column_stats(self, schema: str, table: str, columns: list[str]) -> dict[str, ColumnExactStats]:
        raise NotImplementedError("Implemented in a later phase")

    @with_timeout(settings.PROFILING_QUERY_TIMEOUT_SECONDS)
    def sample_rows(
        self, schema: str, table: str, sample_size: int, row_count_estimate: int | None = None
    ) -> SampleResult:
        # T-SQL has no LIMIT clause — TOP (?) is the parameterized equivalent.
        # row_count_estimate: accepted (every caller passes it) but unused —
        # unlike PostgreSQLProvider, this provider has no size-aware sampling
        # strategy (e.g. TABLESAMPLE) implemented yet; TOP (?) is used as-is
        # regardless of table size.
        try:
            cur = self._connection().cursor()
            query = f"SELECT TOP (?) * FROM {_quote_ident(schema)}.{_quote_ident(table)}"
            cur.execute(query, sample_size)
            rows = _rows_as_dicts(cur)
            # TOP (N) is the ONLY truncation mechanism this query uses — no
            # TABLESAMPLE, no probabilistic/estimate-driven branching (unlike
            # PostgreSQLProvider). It deterministically returns
            # min(N, actual_row_count) rows, so fewer rows than requested
            # coming back is conclusive proof the table has no more rows to
            # give — a genuine full scan, not a guess. Exactly N rows back is
            # the ambiguous case (the table may have more): stays False.
            return SampleResult(rows=rows, is_full_scan=len(rows) < sample_size)
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def fetch_rows_by_keys(self, schema: str, table: str, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Exact, parameterized lookup by one or more key-column combinations
        (single-column or composite). T-SQL has no row-value `(a, b) IN
        ((1, 2), (3, 4))` syntax (unlike PostgreSQL's real implementation in
        postgresql_provider.py), so the portable, still fully parameterized
        equivalent is an OR-of-ANDs: `(col1 = ? AND col2 = ?) OR (col1 = ? AND
        col2 = ?) OR ...`. Every VALUE is bound as a `?` placeholder — never
        string-interpolated; only identifiers (schema/table/column names) are
        interpolated, and only through `_quote_ident`'s bracket-escaping, the
        same helper `sample_rows` already uses. A None-valued key component
        becomes `col IS NULL` (bound `=` is never true against NULL in SQL,
        so this is required for correctness, not just an edge-case nicety).

        Every dict in `keys` must share the exact same set of key column
        names — mirrors PostgreSQLProvider's own implicit assumption
        (documented there as "true by construction for one staging attempt
        against one dataset's fixed key strategy"), but enforced explicitly
        here with a clear error rather than left to an incidental KeyError.
        """
        if not keys:
            return []

        key_columns = list(keys[0].keys())
        if not key_columns:
            raise SourceQueryError("fetch_rows_by_keys: a key dict must name at least one key column")
        for key_dict in keys:
            if list(key_dict.keys()) != key_columns:
                raise SourceQueryError(
                    "fetch_rows_by_keys: every key dict must share the same set of key column names"
                )

        try:
            cur = self._connection().cursor()

            group_clauses = []
            params: list[Any] = []
            for key_dict in keys:
                col_clauses = []
                for col in key_columns:
                    value = key_dict[col]
                    if value is None:
                        col_clauses.append(f"{_quote_ident(col)} IS NULL")
                    else:
                        col_clauses.append(f"{_quote_ident(col)} = ?")
                        params.append(value)
                group_clauses.append("(" + " AND ".join(col_clauses) + ")")

            order_by = ", ".join(_quote_ident(col) for col in key_columns)
            query = (
                f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)} "
                f"WHERE {' OR '.join(group_clauses)} "
                f"ORDER BY {order_by}"
            )
            cur.execute(query, params)
            return _rows_as_dicts(cur)
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def count_rows(self, schema: str, table: str) -> int:
        try:
            cur = self._connection().cursor()
            cur.execute(f"SELECT COUNT(*) FROM {_quote_ident(schema)}.{_quote_ident(table)}")
            return int(cur.fetchone()[0])
        except pyodbc.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def iter_rows(
        self, schema: str, table: str, columns: list[str], batch_size: int, order_by: list[str] | None = None
    ) -> Iterator[list[dict[str, Any]]]:
        order_columns = order_by if order_by else columns

        @with_timeout(settings.STAGING_MATERIALIZATION_QUERY_TIMEOUT_SECONDS)
        def _fetch_batch(offset: int) -> list[dict[str, Any]]:
            try:
                cur = self._connection().cursor()
                col_list = ", ".join(_quote_ident(c) for c in columns)
                order_list = ", ".join(_quote_ident(c) for c in order_columns)
                # T-SQL requires an ORDER BY for OFFSET/FETCH NEXT — no
                # implicit row order exists otherwise, unlike TOP (?) alone.
                query = (
                    f"SELECT {col_list} FROM {_quote_ident(schema)}.{_quote_ident(table)} "
                    f"ORDER BY {order_list} OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
                )
                cur.execute(query, offset, batch_size)
                return _rows_as_dicts(cur)
            except pyodbc.Error as exc:
                raise SourceQueryError(str(exc)) from exc

        offset = 0
        while True:
            batch = _fetch_batch(offset)
            if not batch:
                return
            yield batch
            if len(batch) < batch_size:
                return
            offset += batch_size

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

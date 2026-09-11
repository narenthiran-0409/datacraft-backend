"""MySQL provider via pymysql.

Only mock-tested in this environment — there is no live MySQL instance
available here. See tests/unit/test_mysql_provider.py.

Note: MySQL has no distinct native BOOLEAN storage type (BOOL/BOOLEAN are
aliases for TINYINT(1)); information_schema.DATA_TYPE reports such columns
as 'tinyint', so they normalize to INTEGER here rather than BOOLEAN. This
is a known MySQL limitation, not a bug.
"""
from __future__ import annotations

import time
from typing import Any

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

# Populated by _import_pymysql() on first use — see postgresql_provider.py
# for why this driver import is deferred rather than a top-level import.
pymysql = None


def _import_pymysql() -> None:
    global pymysql
    if pymysql is not None:
        return
    try:
        import pymysql as _pymysql
    except ImportError as exc:
        raise SourceDriverNotInstalledError(
            "MySQL provider requires the 'pymysql' package, which is not installed."
        ) from exc
    pymysql = _pymysql

_TYPE_MAP = {
    "varchar": "STRING",
    "char": "STRING",
    "text": "TEXT",
    "tinytext": "TEXT",
    "mediumtext": "TEXT",
    "longtext": "TEXT",
    "tinyint": "INTEGER",
    "smallint": "INTEGER",
    "mediumint": "INTEGER",
    "int": "INTEGER",
    "bigint": "INTEGER",
    "decimal": "DECIMAL",
    "numeric": "DECIMAL",
    "float": "DECIMAL",
    "double": "DECIMAL",
    "date": "DATE",
    "datetime": "DATETIME",
    "timestamp": "DATETIME",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
}


def _normalize_type(native_type: str) -> str:
    return _TYPE_MAP.get(native_type.lower(), "STRING")


def _quote_ident(name: str) -> str:
    """Backtick-quotes a MySQL identifier, doubling any internal backtick —
    the standard MySQL escaping rule. schema/table names here come from
    this dataset's own discovered metadata (ultimately sourced from the
    live database's own catalog, not raw HTTP input), but this is still a
    real SQL-injection surface if interpolated unquoted, so it's quoted
    the same way every other identifier-bearing query in this project is."""
    return "`" + name.replace("`", "``") + "`"


def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


class MySQLProvider(SourceDatabaseProvider):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str | None,
        username: str,
        password: str,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        **_kwargs: Any,
    ) -> None:
        _import_pymysql()
        self._host = host
        self._port = port
        self._database = database
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._conn: pymysql.connections.Connection | None = None

    def _connect(self) -> "pymysql.connections.Connection":
        try:
            return pymysql.connect(
                host=self._host,
                port=self._port,
                user=self._username,
                password=self._password,
                database=self._database,
                connect_timeout=self._connect_timeout,
            )
        except pymysql.MySQLError as exc:
            self._translate_error(exc)

    def _connection(self) -> "pymysql.connections.Connection":
        if self._conn is None or not self._conn.open:
            self._conn = self._connect()
        return self._conn

    def _translate_error(self, exc: Exception) -> None:
        message = str(exc).lower()

        if "access denied" in message:
            raise SourceAuthenticationError(f"Authentication failed for user '{self._username}'") from exc
        if "timed out" in message or "timeout" in message:
            raise SourceTimeoutError(
                f"Connection to {self._host}:{self._port} timed out after {self._connect_timeout}s"
            ) from exc
        if "ssl" in message or "tls" in message:
            raise SourceSSLError(f"SSL/TLS negotiation failed with {self._host}:{self._port}") from exc
        if "can't connect" in message or "connection refused" in message or "unknown host" in message:
            raise SourceUnreachableError(f"Could not reach {self._host}:{self._port}") from exc

        raise SourceQueryError(str(exc)) from exc

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        conn = None
        try:
            conn = pymysql.connect(
                host=self._host,
                port=self._port,
                user=self._username,
                password=self._password,
                database=self._database,
                connect_timeout=self._connect_timeout,
            )
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except pymysql.MySQLError as exc:
            self._translate_error(exc)
        else:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ConnectionTestResult(status="HEALTHY", latency_ms=latency_ms, message="Connection successful")
        finally:
            if conn is not None:
                conn.close()

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_foreign_keys=True, supports_exact_row_count=False, supports_views=True)

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_schemas(self) -> list[str]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT SCHEMA_NAME FROM information_schema.SCHEMATA
                    WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
                    ORDER BY SCHEMA_NAME
                    """
                )
                return [row[0] for row in cur.fetchall()]
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_datasets(self, schema: str) -> list[dict[str, Any]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
                    ORDER BY TABLE_NAME
                    """,
                    (schema,),
                )
                return [
                    {"name": name, "object_type": "TABLE" if table_type == "BASE TABLE" else "VIEW"}
                    for name, table_type in cur.fetchall()
                ]
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_columns(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME, ORDINAL_POSITION, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH,
                           NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                    """,
                    (schema, dataset),
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
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_primary_keys(self, schema: str, dataset: str) -> list[str]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = 'PRIMARY'
                    ORDER BY SEQ_IN_INDEX
                    """,
                    (schema, dataset),
                )
                return [row[0] for row in cur.fetchall()]
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_foreign_keys(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT COLUMN_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL
                    """,
                    (schema, dataset),
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
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_row_count(self, schema: str, dataset: str) -> int | None:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT TABLE_ROWS FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    """,
                    (schema, dataset),
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    return None
                return int(row[0])
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    def get_dataset_column_stats(self, schema: str, table: str, columns: list[str]) -> dict[str, ColumnExactStats]:
        raise NotImplementedError("Implemented in a later phase")

    @with_timeout(settings.PROFILING_QUERY_TIMEOUT_SECONDS)
    def sample_rows(
        self, schema: str, table: str, sample_size: int, row_count_estimate: int | None = None
    ) -> SampleResult:
        # row_count_estimate: accepted (every caller passes it) but unused —
        # unlike PostgreSQLProvider, this provider has no size-aware sampling
        # strategy implemented yet; a plain LIMIT is used regardless of table size.
        try:
            with self._connection().cursor() as cur:
                query = f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)} LIMIT %s"
                cur.execute(query, (sample_size,))
                return SampleResult(rows=_rows_as_dicts(cur), is_full_scan=False)
        except pymysql.MySQLError as exc:
            raise SourceQueryError(str(exc)) from exc

    def fetch_rows_by_keys(self, schema: str, table: str, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in a later phase")

    def close(self) -> None:
        if self._conn is not None and self._conn.open:
            self._conn.close()

"""SAP HANA provider via hdbcli.

Only mock-tested in this environment — there is no live SAP HANA instance
available here. See tests/unit/test_saphana_provider.py.
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

# Populated by _import_hdbcli() on first use — see postgresql_provider.py
# for why this driver import is deferred rather than a top-level import.
dbapi = None


def _import_hdbcli() -> None:
    global dbapi
    if dbapi is not None:
        return
    try:
        from hdbcli import dbapi as _dbapi
    except ImportError as exc:
        raise SourceDriverNotInstalledError(
            "SAP HANA provider requires the 'hdbcli' package, which is not installed."
        ) from exc
    dbapi = _dbapi

_TYPE_MAP = {
    "VARCHAR": "STRING",
    "NVARCHAR": "STRING",
    "CHAR": "STRING",
    "NCHAR": "STRING",
    "SHORTTEXT": "STRING",
    "ALPHANUM": "STRING",
    "TEXT": "TEXT",
    "CLOB": "TEXT",
    "NCLOB": "TEXT",
    "TINYINT": "INTEGER",
    "SMALLINT": "INTEGER",
    "INTEGER": "INTEGER",
    "BIGINT": "INTEGER",
    "DECIMAL": "DECIMAL",
    "SMALLDECIMAL": "DECIMAL",
    "REAL": "DECIMAL",
    "DOUBLE": "DECIMAL",
    "FLOAT": "DECIMAL",
    "DATE": "DATE",
    "TIME": "DATETIME",
    "TIMESTAMP": "DATETIME",
    "SECONDDATE": "DATETIME",
    "BOOLEAN": "BOOLEAN",
}


def _normalize_type(native_type: str) -> str:
    return _TYPE_MAP.get((native_type or "").upper(), "STRING")


def _quote_ident(name: str) -> str:
    """Double-quotes a SAP HANA identifier, doubling any internal double
    quote — the standard ANSI/HANA escaping rule (same as Oracle's)."""
    return '"' + name.replace('"', '""') + '"'


def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


class SAPHanaProvider(SourceDatabaseProvider):
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
        _import_hdbcli()
        self._host = host
        self._port = port
        self._database = database
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._conn: dbapi.Connection | None = None

    def _connect_kwargs(self) -> dict:
        kwargs = {
            "address": self._host,
            "port": self._port,
            "user": self._username,
            "password": self._password,
            "communicationTimeout": self._connect_timeout * 1000,
        }
        if self._database:
            kwargs["databaseName"] = self._database
        return kwargs

    def _connect(self) -> dbapi.Connection:
        try:
            return dbapi.connect(**self._connect_kwargs())
        except dbapi.Error as exc:
            self._translate_error(exc)

    def _connection(self) -> dbapi.Connection:
        if self._conn is None or not self._conn.isconnected():
            self._conn = self._connect()
        return self._conn

    def _translate_error(self, exc: Exception) -> None:
        message = str(exc).lower()

        if "invalid username or password" in message or "authentication failed" in message:
            raise SourceAuthenticationError(f"Authentication failed for user '{self._username}'") from exc
        if "timeout" in message or "timed out" in message:
            raise SourceTimeoutError(
                f"Connection to {self._host}:{self._port} timed out after {self._connect_timeout}s"
            ) from exc
        if "ssl" in message or "tls" in message:
            raise SourceSSLError(f"SSL/TLS negotiation failed with {self._host}:{self._port}") from exc
        if "cannot connect" in message or "connection refused" in message or "could not connect" in message:
            raise SourceUnreachableError(f"Could not reach {self._host}:{self._port}") from exc

        raise SourceQueryError(str(exc)) from exc

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        conn = None
        try:
            conn = dbapi.connect(**self._connect_kwargs())
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM DUMMY")
            cur.fetchone()
        except dbapi.Error as exc:
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
            cur = self._connection().cursor()
            cur.execute(
                "SELECT SCHEMA_NAME FROM SYS.SCHEMAS "
                "WHERE SCHEMA_NAME NOT LIKE '\\_SYS%' ESCAPE '\\' AND SCHEMA_NAME NOT IN ('SYS', 'PUBLIC') "
                "ORDER BY SCHEMA_NAME"
            )
            return [row[0] for row in cur.fetchall()]
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_datasets(self, schema: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT TABLE_NAME, 'TABLE' FROM SYS.TABLES WHERE SCHEMA_NAME = ?
                UNION ALL
                SELECT VIEW_NAME, 'VIEW' FROM SYS.VIEWS WHERE SCHEMA_NAME = ?
                ORDER BY 1
                """,
                (schema, schema),
            )
            return [{"name": name, "object_type": object_type} for name, object_type in cur.fetchall()]
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_columns(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME, POSITION, DATA_TYPE_NAME, LENGTH, SCALE, IS_NULLABLE
                FROM SYS.TABLE_COLUMNS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?
                ORDER BY POSITION
                """,
                (schema, dataset),
            )
            return [
                {
                    "name": name,
                    "ordinal_position": position,
                    "native_data_type": data_type_name,
                    "normalized_data_type": _normalize_type(data_type_name),
                    "max_length": length,
                    "numeric_precision": length if _normalize_type(data_type_name) == "DECIMAL" else None,
                    "numeric_scale": scale,
                    "is_nullable": is_nullable == "TRUE",
                }
                for name, position, data_type_name, length, scale, is_nullable in cur.fetchall()
            ]
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_primary_keys(self, schema: str, dataset: str) -> list[str]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME FROM SYS.CONSTRAINTS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ? AND IS_PRIMARY_KEY = 'TRUE'
                ORDER BY POSITION
                """,
                (schema, dataset),
            )
            return [row[0] for row in cur.fetchall()]
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_foreign_keys(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME, REFERENCED_SCHEMA_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                FROM SYS.REFERENTIAL_CONSTRAINTS
                WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?
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
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_row_count(self, schema: str, dataset: str) -> int | None:
        try:
            cur = self._connection().cursor()
            cur.execute(
                "SELECT RECORD_COUNT FROM SYS.M_TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
                (schema, dataset),
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def get_dataset_column_stats(self, schema: str, table: str, columns: list[str]) -> dict[str, ColumnExactStats]:
        raise NotImplementedError("Implemented in a later phase")

    @with_timeout(settings.PROFILING_QUERY_TIMEOUT_SECONDS)
    def sample_rows(self, schema: str, table: str, sample_size: int) -> SampleResult:
        try:
            cur = self._connection().cursor()
            query = f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)} LIMIT ?"
            cur.execute(query, (sample_size,))
            return SampleResult(rows=_rows_as_dicts(cur), is_full_scan=False)
        except dbapi.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def fetch_rows_by_keys(self, schema: str, table: str, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in a later phase")

    def close(self) -> None:
        if self._conn is not None and self._conn.isconnected():
            self._conn.close()

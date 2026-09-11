"""Oracle provider via python-oracledb, in THIN mode (no Oracle Instant
Client required on the host — oracledb.connect() runs in thin mode by
default unless oracledb.init_oracle_client() is explicitly called, which
this provider deliberately never does).

Only mock-tested in this environment — there is no live Oracle instance
available here. See tests/unit/test_oracle_provider.py.

Oracle has no per-database "schema" concept distinct from the connecting
user in the way Postgres/SQL Server/MySQL do — a "schema" here means an
object owner (ALL_TABLES.OWNER). list_schemas() enumerates owners that
actually own at least one table/view, visible to the connecting user,
excluding well-known Oracle-maintained system schemas.
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

# Populated by _import_oracledb() on first use — see postgresql_provider.py
# for why this driver import is deferred rather than a top-level import.
oracledb = None


def _import_oracledb() -> None:
    global oracledb
    if oracledb is not None:
        return
    try:
        import oracledb as _oracledb
    except ImportError as exc:
        raise SourceDriverNotInstalledError(
            "Oracle provider requires the 'oracledb' package, which is not installed."
        ) from exc
    oracledb = _oracledb

_SYSTEM_SCHEMAS = {
    "SYS", "SYSTEM", "OUTLN", "XDB", "ORDSYS", "ORDDATA", "CTXSYS", "MDSYS", "WMSYS",
    "APPQOSSYS", "DBSNMP", "GSMADMIN_INTERNAL", "APEX_040000", "APEX_PUBLIC_USER",
    "ANONYMOUS", "DIP", "FLOWS_FILES", "MDDATA", "OJVMSYS", "LBACSYS", "AUDSYS",
    "GSMCATUSER", "GSMUSER", "REMOTE_SCHEDULER_AGENT", "SYSBACKUP", "SYSDG", "SYSKM",
    "SYSRAC", "PDBADMIN",
}

_TYPE_MAP_SIMPLE = {
    "VARCHAR2": "STRING",
    "NVARCHAR2": "STRING",
    "CHAR": "STRING",
    "NCHAR": "STRING",
    "CLOB": "TEXT",
    "NCLOB": "TEXT",
    "LONG": "TEXT",
    "FLOAT": "DECIMAL",
    "BINARY_FLOAT": "DECIMAL",
    "BINARY_DOUBLE": "DECIMAL",
    "DATE": "DATETIME",
    "TIMESTAMP": "DATETIME",
    "BOOLEAN": "BOOLEAN",
}


def _normalize_type(native_type: str, data_scale: int | None) -> str:
    native_type = (native_type or "").upper()
    if native_type == "NUMBER":
        return "INTEGER" if not data_scale else "DECIMAL"
    if native_type.startswith("TIMESTAMP"):
        return "DATETIME"
    return _TYPE_MAP_SIMPLE.get(native_type, "STRING")


def _quote_ident(name: str) -> str:
    """Double-quotes an Oracle identifier, doubling any internal double
    quote — the standard Oracle escaping rule, and also preserves exact
    case (an unquoted identifier would be folded to uppercase, which could
    mismatch the case ALL_TABLES/ALL_TAB_COLUMNS actually reported)."""
    return '"' + name.replace('"', '""') + '"'


def _rows_as_dicts(cur) -> list[dict[str, Any]]:
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


class OracleProvider(SourceDatabaseProvider):
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
        _import_oracledb()
        self._host = host
        self._port = port
        self._service_name = database
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._conn: oracledb.Connection | None = None

    def _dsn(self) -> str:
        return f"{self._host}:{self._port}/{self._service_name or 'ORCL'}"

    def _connect(self) -> oracledb.Connection:
        try:
            return oracledb.connect(
                user=self._username,
                password=self._password,
                dsn=self._dsn(),
                tcp_connect_timeout=self._connect_timeout,
            )
        except oracledb.Error as exc:
            self._translate_error(exc)

    def _connection(self) -> oracledb.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _translate_error(self, exc: Exception) -> None:
        message = str(exc).lower()

        if "ora-01017" in message or "invalid username" in message:
            raise SourceAuthenticationError(f"Authentication failed for user '{self._username}'") from exc
        if "ora-12170" in message or "timeout" in message or "timed out" in message:
            raise SourceTimeoutError(
                f"Connection to {self._host}:{self._port} timed out after {self._connect_timeout}s"
            ) from exc
        if "ssl" in message or "tls" in message or "ora-28860" in message:
            raise SourceSSLError(f"SSL/TLS negotiation failed with {self._host}:{self._port}") from exc
        if (
            "ora-12154" in message
            or "ora-12541" in message
            or "ora-12545" in message
            or "could not resolve" in message
            or "no listener" in message
        ):
            raise SourceUnreachableError(f"Could not reach {self._host}:{self._port}") from exc

        raise SourceQueryError(str(exc)) from exc

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        conn = None
        try:
            conn = oracledb.connect(
                user=self._username,
                password=self._password,
                dsn=self._dsn(),
                tcp_connect_timeout=self._connect_timeout,
            )
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM DUAL")
            cur.fetchone()
        except oracledb.Error as exc:
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
            cur.execute("SELECT DISTINCT OWNER FROM ALL_TABLES ORDER BY OWNER")
            return [row[0] for row in cur.fetchall() if row[0] not in _SYSTEM_SCHEMAS]
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_datasets(self, schema: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute("SELECT TABLE_NAME FROM ALL_TABLES WHERE OWNER = :owner", owner=schema)
            tables = [{"name": row[0], "object_type": "TABLE"} for row in cur.fetchall()]

            cur.execute("SELECT VIEW_NAME FROM ALL_VIEWS WHERE OWNER = :owner", owner=schema)
            views = [{"name": row[0], "object_type": "VIEW"} for row in cur.fetchall()]

            return sorted(tables + views, key=lambda d: d["name"])
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_columns(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME, COLUMN_ID, DATA_TYPE, CHAR_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE
                FROM ALL_TAB_COLUMNS
                WHERE OWNER = :owner AND TABLE_NAME = :table_name
                ORDER BY COLUMN_ID
                """,
                owner=schema,
                table_name=dataset,
            )
            return [
                {
                    "name": name,
                    "ordinal_position": ordinal_position,
                    "native_data_type": data_type,
                    "normalized_data_type": _normalize_type(data_type, data_scale),
                    "max_length": char_length,
                    "numeric_precision": data_precision,
                    "numeric_scale": data_scale,
                    "is_nullable": nullable == "Y",
                }
                for name, ordinal_position, data_type, char_length, data_precision, data_scale, nullable
                in cur.fetchall()
            ]
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_primary_keys(self, schema: str, dataset: str) -> list[str]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT acc.COLUMN_NAME
                FROM ALL_CONSTRAINTS ac
                JOIN ALL_CONS_COLUMNS acc ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME AND ac.OWNER = acc.OWNER
                WHERE ac.CONSTRAINT_TYPE = 'P' AND ac.OWNER = :owner AND ac.TABLE_NAME = :table_name
                ORDER BY acc.POSITION
                """,
                owner=schema,
                table_name=dataset,
            )
            return [row[0] for row in cur.fetchall()]
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_foreign_keys(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            cur = self._connection().cursor()
            cur.execute(
                """
                SELECT acc.COLUMN_NAME, r_ac.OWNER, r_ac.TABLE_NAME, r_acc.COLUMN_NAME
                FROM ALL_CONSTRAINTS ac
                JOIN ALL_CONS_COLUMNS acc ON ac.CONSTRAINT_NAME = acc.CONSTRAINT_NAME AND ac.OWNER = acc.OWNER
                JOIN ALL_CONSTRAINTS r_ac ON ac.R_CONSTRAINT_NAME = r_ac.CONSTRAINT_NAME AND ac.R_OWNER = r_ac.OWNER
                JOIN ALL_CONS_COLUMNS r_acc
                  ON r_ac.CONSTRAINT_NAME = r_acc.CONSTRAINT_NAME
                 AND r_ac.OWNER = r_acc.OWNER
                 AND acc.POSITION = r_acc.POSITION
                WHERE ac.CONSTRAINT_TYPE = 'R' AND ac.OWNER = :owner AND ac.TABLE_NAME = :table_name
                """,
                owner=schema,
                table_name=dataset,
            )
            return [
                {
                    "column": column_name,
                    "referenced_schema": ref_owner,
                    "referenced_table": ref_table,
                    "referenced_column": ref_column,
                }
                for column_name, ref_owner, ref_table, ref_column in cur.fetchall()
            ]
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_row_count(self, schema: str, dataset: str) -> int | None:
        try:
            cur = self._connection().cursor()
            cur.execute(
                "SELECT NUM_ROWS FROM ALL_TABLES WHERE OWNER = :owner AND TABLE_NAME = :table_name",
                owner=schema,
                table_name=dataset,
            )
            row = cur.fetchone()
            if row is None or row[0] is None:
                return None
            return int(row[0])
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def get_dataset_column_stats(self, schema: str, table: str, columns: list[str]) -> dict[str, ColumnExactStats]:
        raise NotImplementedError("Implemented in a later phase")

    @with_timeout(settings.PROFILING_QUERY_TIMEOUT_SECONDS)
    def sample_rows(
        self, schema: str, table: str, sample_size: int, row_count_estimate: int | None = None
    ) -> SampleResult:
        # row_count_estimate: accepted (every caller passes it) but unused —
        # unlike PostgreSQLProvider, this provider has no size-aware sampling
        # strategy implemented yet; FETCH FIRST is used as-is regardless of table size.
        try:
            cur = self._connection().cursor()
            query = (
                f"SELECT * FROM {_quote_ident(schema)}.{_quote_ident(table)} "
                "FETCH FIRST :row_limit ROWS ONLY"
            )
            cur.execute(query, row_limit=sample_size)
            return SampleResult(rows=_rows_as_dicts(cur), is_full_scan=False)
        except oracledb.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def fetch_rows_by_keys(self, schema: str, table: str, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError("Implemented in a later phase")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

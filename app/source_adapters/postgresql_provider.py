from __future__ import annotations

import time
from typing import Any

from app.core.config import settings
from app.source_adapters.base import (
    ColumnExactStats,
    ConnectionTestResult,
    ProviderCapabilities,
    SampleResult,
    SourceDatabaseProvider,
)
from app.source_adapters.exceptions import (
    ExactStatsTimeoutError,
    SourceAuthenticationError,
    SourceDriverNotInstalledError,
    SourceQueryError,
    SourceSSLError,
    SourceTimeoutError,
    SourceUnreachableError,
)
from app.source_adapters.timeout import with_timeout

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5

# Populated by _import_psycopg() on first use — psycopg is an optional
# vendor driver, not a hard dependency of the whole application. Importing
# it at module load time would mean starting the app (or importing any
# module that transitively imports this one) requires psycopg to be
# installed even for a deployment that never uses PostgreSQL.
psycopg = None
OperationalError = None
QueryCanceled = None
sql = None
dict_row = None


def _import_psycopg() -> None:
    global psycopg, OperationalError, QueryCanceled, sql, dict_row
    if psycopg is not None:
        return
    try:
        import psycopg as _psycopg
        from psycopg import OperationalError as _OperationalError
        from psycopg import sql as _sql
        from psycopg.errors import QueryCanceled as _QueryCanceled
        from psycopg.rows import dict_row as _dict_row
    except ImportError as exc:
        raise SourceDriverNotInstalledError(
            "PostgreSQL provider requires the 'psycopg' package, which is not installed."
        ) from exc
    psycopg, OperationalError, QueryCanceled, sql, dict_row = _psycopg, _OperationalError, _QueryCanceled, _sql, _dict_row

_TYPE_MAP = {
    "text": "TEXT",
    "character varying": "STRING",
    "varchar": "STRING",
    "character": "STRING",
    "char": "STRING",
    "bpchar": "STRING",
    "integer": "INTEGER",
    "int": "INTEGER",
    "int2": "INTEGER",
    "int4": "INTEGER",
    "int8": "INTEGER",
    "smallint": "INTEGER",
    "bigint": "INTEGER",
    "numeric": "DECIMAL",
    "decimal": "DECIMAL",
    "real": "DECIMAL",
    "double precision": "DECIMAL",
    "float4": "DECIMAL",
    "float8": "DECIMAL",
    "date": "DATE",
    "timestamp": "DATETIME",
    "timestamp without time zone": "DATETIME",
    "timestamp with time zone": "DATETIME",
    "timestamptz": "DATETIME",
    "boolean": "BOOLEAN",
    "bool": "BOOLEAN",
}


def _normalize_type(native_type: str) -> str:
    return _TYPE_MAP.get(native_type.lower(), "STRING")


class PostgreSQLProvider(SourceDatabaseProvider):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str | None,
        username: str,
        password: str,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        _import_psycopg()
        self._host = host
        self._port = port
        self._database = database
        self._username = username
        self._password = password
        self._connect_timeout = connect_timeout
        self._conn: psycopg.Connection | None = None

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(
                    host=self._host,
                    port=self._port,
                    dbname=self._database or "postgres",
                    user=self._username,
                    password=self._password,
                    connect_timeout=self._connect_timeout,
                )
            except OperationalError as exc:
                self._translate_operational_error(exc)
        return self._conn

    def test_connection(self) -> ConnectionTestResult:
        started = time.monotonic()
        try:
            with psycopg.connect(
                host=self._host,
                port=self._port,
                dbname=self._database or "postgres",
                user=self._username,
                password=self._password,
                connect_timeout=self._connect_timeout,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
        except OperationalError as exc:
            self._translate_operational_error(exc)
        else:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ConnectionTestResult(status="HEALTHY", latency_ms=latency_ms, message="Connection successful")

    def _translate_operational_error(self, exc: OperationalError) -> None:
        message = str(exc).lower()

        if "password authentication failed" in message or "authentication failed" in message:
            raise SourceAuthenticationError(f"Authentication failed for user '{self._username}'") from exc
        if "timeout expired" in message or "timed out" in message:
            raise SourceTimeoutError(
                f"Connection to {self._host}:{self._port} timed out after {self._connect_timeout}s"
            ) from exc
        if "ssl" in message or "sslmode" in message:
            raise SourceSSLError(f"SSL negotiation failed with {self._host}:{self._port}") from exc
        if (
            "could not translate host name" in message
            or "could not connect to server" in message
            or "connection refused" in message
            or "no route to host" in message
        ):
            raise SourceUnreachableError(f"Could not reach {self._host}:{self._port}") from exc

        raise SourceQueryError(str(exc)) from exc

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supports_foreign_keys=True,
            supports_exact_row_count=False,
            supports_views=True,
            supports_native_sampling=True,
        )

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_schemas(self) -> list[str]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT schema_name FROM information_schema.schemata
                    WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
                      AND schema_name NOT LIKE 'pg_toast%%'
                      AND schema_name NOT LIKE 'pg_temp%%'
                    ORDER BY schema_name
                    """
                )
                return [row[0] for row in cur.fetchall()]
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def list_datasets(self, schema: str) -> list[dict[str, Any]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name, table_type FROM information_schema.tables
                    WHERE table_schema = %s AND table_type IN ('BASE TABLE', 'VIEW')
                    ORDER BY table_name
                    """,
                    (schema,),
                )
                return [
                    {"name": name, "object_type": "TABLE" if table_type == "BASE TABLE" else "VIEW"}
                    for name, table_type in cur.fetchall()
                ]
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_columns(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT column_name, ordinal_position, data_type, character_maximum_length,
                           numeric_precision, numeric_scale, is_nullable
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
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
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_primary_keys(self, schema: str, dataset: str) -> list[str]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT a.attname
                    FROM pg_index i
                    JOIN pg_class c ON c.oid = i.indrelid
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    JOIN unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
                    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
                    WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary
                    ORDER BY k.ord
                    """,
                    (schema, dataset),
                )
                return [row[0] for row in cur.fetchall()]
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_foreign_keys(self, schema: str, dataset: str) -> list[dict[str, Any]]:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT kcu.column_name, ccu.table_schema, ccu.table_name, ccu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage ccu
                      ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = %s AND tc.table_name = %s
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
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.DISCOVERY_QUERY_TIMEOUT_SECONDS)
    def get_row_count(self, schema: str, dataset: str) -> int | None:
        try:
            with self._connection().cursor() as cur:
                cur.execute(
                    """
                    SELECT c.reltuples
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s AND c.relname = %s
                    """,
                    (schema, dataset),
                )
                row = cur.fetchone()
                if row is None or row[0] is None or row[0] < 0:
                    return None
                return int(round(row[0]))
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def get_dataset_column_stats(self, schema: str, table: str, columns: list[str]) -> dict[str, ColumnExactStats]:
        results: dict[str, ColumnExactStats] = {}
        batch_size = settings.PROFILING_EXACT_STATS_MAX_COLUMNS_PER_QUERY
        for i in range(0, len(columns), batch_size):
            results.update(self._exact_stats_batch(schema, table, columns[i : i + batch_size]))
        return results

    def _exact_stats_batch(self, schema: str, table: str, columns: list[str]) -> dict[str, ColumnExactStats]:
        select_parts = [sql.SQL("COUNT(*) AS total_row_count")]
        for idx, col in enumerate(columns):
            select_parts.append(
                sql.SQL("COUNT({col}) AS {alias}").format(col=sql.Identifier(col), alias=sql.Identifier(f"c{idx}_non_null"))
            )
            select_parts.append(
                sql.SQL("COUNT(DISTINCT {col}) AS {alias}").format(
                    col=sql.Identifier(col), alias=sql.Identifier(f"c{idx}_distinct")
                )
            )

        query = sql.SQL("SELECT {fields} FROM {schema}.{table}").format(
            fields=sql.SQL(", ").join(select_parts),
            schema=sql.Identifier(schema),
            table=sql.Identifier(table),
        )

        timeout_ms = int(settings.PROFILING_EXACT_STATS_TIMEOUT_SECONDS * 1000)
        try:
            with self._connection().cursor() as cur:
                cur.execute(f"SET statement_timeout = {timeout_ms}")
                cur.execute(query)
                row = cur.fetchone()
                cur.execute("SET statement_timeout = 0")
        except QueryCanceled as exc:
            # The query's cancellation leaves this connection's transaction
            # aborted — every further command on it (including the "SET
            # statement_timeout = 0" reset, and any later call this provider
            # instance makes, e.g. sample_rows()) would fail with
            # InFailedSqlTransaction until rolled back. Roll back here so
            # the connection is usable again for the rest of this run.
            self._connection().rollback()
            raise ExactStatsTimeoutError(
                f"Exact stats query for {schema}.{table} exceeded {settings.PROFILING_EXACT_STATS_TIMEOUT_SECONDS}s"
            ) from exc
        except psycopg.Error as exc:
            self._connection().rollback()
            raise SourceQueryError(str(exc)) from exc

        total_row_count = row[0]
        results: dict[str, ColumnExactStats] = {}
        for idx, col in enumerate(columns):
            non_null = row[1 + idx * 2]
            distinct = row[2 + idx * 2]
            results[col] = ColumnExactStats(
                null_count=total_row_count - non_null,
                distinct_count=distinct,
                total_row_count=total_row_count,
            )
        return results

    @with_timeout(settings.PROFILING_QUERY_TIMEOUT_SECONDS)
    def sample_rows(
        self, schema: str, table: str, sample_size: int, row_count_estimate: int | None = None
    ) -> SampleResult:
        try:
            with self._connection().cursor(row_factory=dict_row) as cur:
                if row_count_estimate is not None and row_count_estimate > 0 and row_count_estimate <= sample_size:
                    query = sql.SQL("SELECT * FROM {schema}.{table}").format(
                        schema=sql.Identifier(schema), table=sql.Identifier(table)
                    )
                    cur.execute(query)
                    return SampleResult(rows=cur.fetchall(), is_full_scan=True)

                # TABLESAMPLE only pays off once the table is meaningfully
                # larger than the requested sample — for small/medium tables
                # its probabilistic nature (BERNOULLI is row-level, but still
                # a random trial per row) can undershoot the requested size
                # by chance, so a plain LIMIT is both cheaper and
                # deterministic there.
                if row_count_estimate is not None and row_count_estimate > sample_size * 10:
                    # Oversample well above the exact ratio so a LIMIT after
                    # a probabilistic BERNOULLI sample still reliably yields
                    # sample_size rows.
                    percentage = min(100.0, (sample_size / row_count_estimate) * 100 * 3)
                    query = sql.SQL("SELECT * FROM {schema}.{table} TABLESAMPLE BERNOULLI (%s) LIMIT %s").format(
                        schema=sql.Identifier(schema), table=sql.Identifier(table)
                    )
                    cur.execute(query, (percentage, sample_size))
                    return SampleResult(rows=cur.fetchall(), is_full_scan=False)

                query = sql.SQL("SELECT * FROM {schema}.{table} LIMIT %s").format(
                    schema=sql.Identifier(schema), table=sql.Identifier(table)
                )
                cur.execute(query, (sample_size,))
                return SampleResult(rows=cur.fetchall(), is_full_scan=False)
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    @with_timeout(settings.STAGING_QUERY_TIMEOUT_SECONDS)
    def fetch_rows_by_keys(self, schema: str, table: str, keys: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Single batched query for the whole `keys` set — never one query
        per record. Uses Postgres row-value IN syntax:
        WHERE (col1, col2) IN ((v1a, v2a), (v1b, v2b), ...). Every dict in
        `keys` must share the same set of key column names (true by
        construction for one staging attempt against one dataset's fixed
        key strategy)."""
        if not keys:
            return []

        key_columns = list(keys[0].keys())
        try:
            with self._connection().cursor(row_factory=dict_row) as cur:
                col_idents = sql.SQL(", ").join(sql.Identifier(c) for c in key_columns)
                one_row_placeholder = sql.SQL("({})").format(
                    sql.SQL(", ").join(sql.Placeholder() for _ in key_columns)
                )
                all_row_placeholders = sql.SQL(", ").join(one_row_placeholder for _ in keys)

                query = sql.SQL("SELECT * FROM {schema}.{table} WHERE ({cols}) IN ({values})").format(
                    schema=sql.Identifier(schema), table=sql.Identifier(table),
                    cols=col_idents, values=all_row_placeholders,
                )
                params = [key_dict[col] for key_dict in keys for col in key_columns]
                cur.execute(query, params)
                return cur.fetchall()
        except psycopg.Error as exc:
            raise SourceQueryError(str(exc)) from exc

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

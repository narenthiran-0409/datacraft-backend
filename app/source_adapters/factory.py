import importlib

from app.source_adapters.base import SourceDatabaseProvider

# Module path + class name only — resolved lazily by get_provider() so that
# importing this module (or anything that transitively imports it) never
# requires all five providers' native vendor drivers to be installed, only
# the driver for whichever connection type is actually requested.
_PROVIDERS_BY_CODE = {
    "POSTGRESQL": ("app.source_adapters.postgresql_provider", "PostgreSQLProvider"),
    "SQL_SERVER": ("app.source_adapters.sqlserver_provider", "SQLServerProvider"),
    "MYSQL": ("app.source_adapters.mysql_provider", "MySQLProvider"),
    "ORACLE": ("app.source_adapters.oracle_provider", "OracleProvider"),
    "SAP_HANA": ("app.source_adapters.saphana_provider", "SAPHanaProvider"),
}


def get_provider(
    connection_type_code: str,
    *,
    host: str,
    port: int,
    database: str | None,
    username: str,
    password: str,
) -> SourceDatabaseProvider:
    entry = _PROVIDERS_BY_CODE.get(connection_type_code)
    if entry is None:
        raise ValueError(f"Unsupported connection type: {connection_type_code}")

    module_path, class_name = entry
    provider_cls = getattr(importlib.import_module(module_path), class_name)
    return provider_cls(host=host, port=port, database=database, username=username, password=password)

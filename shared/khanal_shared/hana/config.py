"""SAP HANA connection settings from environment and/or SSM."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from khanal_shared.hana.ssm_config import (
    hana_use_ssm,
    load_hana_config_from_ssm,
    load_hana_credentials_from_ssm,
)


@dataclass(frozen=True)
class HanaConnectionParams:
    host: str
    port: int
    user: str
    password: str
    database: str | None = None
    schema: str | None = None
    max_pool_size: int = 5


def quote_hana_identifier(name: str) -> str:
    """Quote a HANA identifier (schema / table) for SQL."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def qualified_table(schema: str | None, table: str, alias: str | None = None) -> str:
    """Return ``"SCHEMA"."TABLE" alias`` or ``TABLE alias`` when schema is unset."""
    table_q = quote_hana_identifier(table)
    if schema:
        base = f"{quote_hana_identifier(schema)}.{table_q}"
    else:
        base = table_q
    if alias:
        return f"{base} {alias}"
    return base


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    return int(raw)


@lru_cache(maxsize=1)
def get_hana_params() -> HanaConnectionParams | None:
    """Return connection params when host + user + password are configured."""
    host = ""
    port = 30015
    user = ""
    password = ""
    database: str | None = None
    schema: str | None = None
    max_pool_size = 5

    if hana_use_ssm():
        cfg = load_hana_config_from_ssm()
        creds = load_hana_credentials_from_ssm()
        host = str(cfg.get("host") or "").strip()
        if cfg.get("port") is not None:
            port = int(cfg["port"])
        user = str(creds.get("username") or creds.get("user") or "").strip()
        password = str(creds.get("password") or "")
        db_val = cfg.get("database") or cfg.get("databaseName")
        if db_val:
            database = str(db_val).strip() or None
        schema_val = cfg.get("schema") or cfg.get("databaseSchema") or cfg.get("hana_schema")
        if schema_val:
            schema = str(schema_val).strip() or None
        if cfg.get("max_pool_size") is not None:
            max_pool_size = int(cfg["max_pool_size"])

    host = host or (os.environ.get("HANA_HOST") or "").strip()
    if os.environ.get("HANA_PORT"):
        port = _env_int("HANA_PORT", port)
    user = user or (os.environ.get("HANA_USER") or os.environ.get("HANA_USERNAME") or "").strip()
    password = password or (os.environ.get("HANA_PASSWORD") or "")
    if not database:
        db_env = (os.environ.get("HANA_DATABASE") or os.environ.get("HANA_DATABASE_NAME") or "").strip()
        database = db_env or None
    if not schema:
        schema_env = (os.environ.get("HANA_SCHEMA") or os.environ.get("HANA_DATABASE_SCHEMA") or "").strip()
        schema = schema_env or None
    max_pool_size = _env_int("HANA_MAX_POOL_SIZE", max_pool_size)

    if not host or not user or not password:
        return None

    return HanaConnectionParams(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        schema=schema,
        max_pool_size=max(1, min(max_pool_size, 10)),
    )


def hana_enabled() -> bool:
    return get_hana_params() is not None

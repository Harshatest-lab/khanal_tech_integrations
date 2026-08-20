"""Runtime credentials: DATABASE_URL from env, Unicommerce API creds from SSM.

Unicommerce API credentials live in one SecureString JSON parameter
(default ``/unicommerce/api_credentials``):
``{"base_url": "...", "username": "...", "password": "...", "facility_code": "..."}``.
Cached in-process for the life of the Lambda container.

SAP Service Layer credentials are NOT handled here -- reused as-is from
``khanal_shared.sap_sl`` (SSM-cached B1 session, default ``/sap/*`` profile
-- see ``unicommerce/__init__.py`` for why that's the correct entity).

Amazon Flex (PRCJ) SP-API credentials live in one SecureString JSON
parameter (default ``/amazon-flex/lwa``): ``{"host": "...",
"marketplace_id": "...", "client_id": "...", "client_secret": "...",
"refresh_token": "..."}`` -- the same working LWA refresh token khanal-oms
already uses for this seller account, reseeded here rather than
re-registering a second Amazon app (see docs/superpowers/specs/
2026-08-20-amazon-flex-sap-pipeline-design.md).

For local runs, copy ``credentials_local.example.py`` to
``credentials_local.py`` (gitignored) -- same pattern as amazon-us-b2c.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


DATABASE_URL = _env("DATABASE_URL")
UC_API_PARAM_NAME = _env("UC_API_PARAM_NAME", "/unicommerce/api_credentials")
FLEX_LWA_PARAM_NAME = _env("FLEX_LWA_PARAM_NAME", "/amazon-flex/lwa")

try:
    from unicommerce.credentials_local import (  # type: ignore[import-not-found]
        DATABASE_URL as _LOCAL_DATABASE_URL,
    )

    if _LOCAL_DATABASE_URL:
        DATABASE_URL = _LOCAL_DATABASE_URL.strip()
except ImportError:
    pass


@dataclass(frozen=True)
class UcCredentials:
    base_url: str
    username: str
    password: str
    facility_code: str


_uc_cache: UcCredentials | None = None


def get_uc_credentials() -> UcCredentials:
    global _uc_cache
    if _uc_cache is not None:
        return _uc_cache

    local = _uc_from_local()
    if local is not None:
        _uc_cache = local
        return _uc_cache

    import boto3

    ssm = boto3.client("ssm")
    raw = ssm.get_parameter(Name=UC_API_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(raw)
    creds = UcCredentials(
        base_url=(data.get("base_url") or "").strip().rstrip("/"),
        username=(data.get("username") or "").strip(),
        password=(data.get("password") or "").strip(),
        facility_code=(data.get("facility_code") or "").strip(),
    )
    if not (creds.base_url and creds.username and creds.password):
        raise RuntimeError(f"SSM {UC_API_PARAM_NAME} is missing base_url/username/password")
    _uc_cache = creds
    return _uc_cache


def _uc_from_local() -> UcCredentials | None:
    try:
        from unicommerce.credentials_local import UC_API  # type: ignore[import-not-found]
    except ImportError:
        return None
    return UcCredentials(
        base_url=UC_API["base_url"].rstrip("/"),
        username=UC_API["username"],
        password=UC_API["password"],
        facility_code=UC_API.get("facility_code", ""),
    )


@dataclass(frozen=True)
class FlexLwaCredentials:
    host: str
    marketplace_id: str
    client_id: str
    client_secret: str
    refresh_token: str


_flex_lwa_cache: FlexLwaCredentials | None = None


def get_flex_lwa_credentials() -> FlexLwaCredentials:
    global _flex_lwa_cache
    if _flex_lwa_cache is not None:
        return _flex_lwa_cache

    local = _flex_lwa_from_local()
    if local is not None:
        _flex_lwa_cache = local
        return _flex_lwa_cache

    import boto3

    ssm = boto3.client("ssm")
    raw = ssm.get_parameter(Name=FLEX_LWA_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(raw)
    creds = FlexLwaCredentials(
        host=(data.get("host") or "").strip().rstrip("/"),
        marketplace_id=(data.get("marketplace_id") or "").strip(),
        client_id=(data.get("client_id") or "").strip(),
        client_secret=(data.get("client_secret") or "").strip(),
        refresh_token=(data.get("refresh_token") or "").strip(),
    )
    if not (creds.host and creds.marketplace_id and creds.client_id and creds.client_secret and creds.refresh_token):
        raise RuntimeError(f"SSM {FLEX_LWA_PARAM_NAME} is missing host/marketplace_id/client_id/client_secret/refresh_token")
    _flex_lwa_cache = creds
    return _flex_lwa_cache


def _flex_lwa_from_local() -> FlexLwaCredentials | None:
    try:
        from unicommerce.credentials_local import FLEX_LWA  # type: ignore[import-not-found]
    except ImportError:
        return None
    return FlexLwaCredentials(
        host=FLEX_LWA["host"].rstrip("/"),
        marketplace_id=FLEX_LWA["marketplace_id"],
        client_id=FLEX_LWA["client_id"],
        client_secret=FLEX_LWA["client_secret"],
        refresh_token=FLEX_LWA["refresh_token"],
    )

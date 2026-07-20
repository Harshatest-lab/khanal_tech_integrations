"""Runtime credentials: DATABASE_URL from env, LWA (SP-API) from SSM.

LWA lives in one SecureString JSON parameter (default ``/amazon-us/lwa``):
``{"client_id": "...", "client_secret": "...", "refresh_token": "..."}``.
Cached in-process for the life of the Lambda container.

For local runs, copy ``credentials_local.example.py`` to
``credentials_local.py`` (gitignored) — same pattern as the Walmart service.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


DATABASE_URL = _env("DATABASE_URL")
LWA_PARAM_NAME = _env("AMZ_LWA_PARAM_NAME", "/amazon-us/lwa")

try:
    from amazon_us.credentials_local import (  # type: ignore[import-not-found]
        DATABASE_URL as _LOCAL_DATABASE_URL,
    )

    if _LOCAL_DATABASE_URL:
        DATABASE_URL = _LOCAL_DATABASE_URL.strip()
except ImportError:
    pass


@dataclass(frozen=True)
class LwaCredentials:
    client_id: str
    client_secret: str
    refresh_token: str


_lwa_cache: LwaCredentials | None = None


def get_lwa_credentials() -> LwaCredentials:
    global _lwa_cache
    if _lwa_cache is not None:
        return _lwa_cache

    local = _lwa_from_local()
    if local is not None:
        _lwa_cache = local
        return _lwa_cache

    import boto3

    ssm = boto3.client("ssm")
    raw = ssm.get_parameter(Name=LWA_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(raw)
    creds = LwaCredentials(
        client_id=(data.get("client_id") or "").strip(),
        client_secret=(data.get("client_secret") or "").strip(),
        refresh_token=(data.get("refresh_token") or "").strip(),
    )
    if not (creds.client_id and creds.client_secret and creds.refresh_token):
        raise RuntimeError(f"SSM {LWA_PARAM_NAME} is missing client_id/client_secret/refresh_token")
    _lwa_cache = creds
    return _lwa_cache


def _lwa_from_local() -> LwaCredentials | None:
    try:
        from amazon_us.credentials_local import LWA  # type: ignore[import-not-found]
    except ImportError:
        return None
    return LwaCredentials(
        client_id=LWA["client_id"], client_secret=LWA["client_secret"], refresh_token=LWA["refresh_token"]
    )

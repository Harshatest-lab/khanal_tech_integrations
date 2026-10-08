"""Runtime credentials: LWA (SP-API), RDS Postgres, and SAP B1 Service Layer,
from SSM or env vars.

Mirrors amazon-us-b2c's ``credentials.py`` pattern (SSM-backed, cached
in-process). HANA already has its own SSM support in ``khanal_shared.hana``
(flip ``HANA_USE_SSM`` + ``HANA_PROFILE``, no code change needed here).

SAP SL deliberately does NOT use ``khanal_shared.sap_sl.ssm_session`` (the
shared SSM *session* cache) — this service does one fresh Login per cron
run on purpose (see ``sap_client.py``), so it never touches or churns the
shared ``/sap/inc*`` B1 session the Walmart / US pipelines cache there.
Only the credentials (not the session) come from SSM.

SSM is opt-in per credential via ``AMZ_UK_LWA_USE_SSM`` / ``AMZ_UK_DB_USE_SSM``
/ ``SAP_SL_USE_SSM`` (``1``/``true``/``yes``/``on``); unset falls back to the
plain env vars (``LWA_CLIENT_ID`` etc. / ``DB_HOST`` etc. / ``SAP_SL_BASE_URL``
etc.) for local dev via ``.env``.

Parameters (SecureString JSON)::

    /amazon-uk/lwa              — client_id, client_secret, refresh_token
    /amazon-uk/db_config        — host, port, dbname, user, password
    /sap/{profile}/sl_config    — sap_b1_url, companydb   (SAP_SL_PROFILE, default "uk")
    /sap/{profile}/credentials  — username, password
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _use_ssm(flag_name: str) -> bool:
    return _env(flag_name).lower() in ("1", "true", "yes", "on")


LWA_PARAM_NAME = _env("AMZ_UK_LWA_PARAM_NAME", "/amazon-uk/lwa")
DB_PARAM_NAME = _env("AMZ_UK_DB_PARAM_NAME", "/amazon-uk/db_config")


def _sap_sl_ssm_prefix() -> str:
    profile = _env("SAP_SL_PROFILE").lower()
    if not profile or profile in ("default", "sap"):
        return "/sap"
    return f"/sap/{profile}"


def _get_ssm_json(name: str) -> dict:
    import boto3

    ssm = boto3.client("ssm")
    raw = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"SSM {name} is not a JSON object")
    return data


@dataclass(frozen=True)
class LwaCredentials:
    client_id: str
    client_secret: str
    refresh_token: str


@lru_cache(maxsize=1)
def get_lwa_credentials() -> LwaCredentials:
    if _use_ssm("AMZ_UK_LWA_USE_SSM"):
        data = _get_ssm_json(LWA_PARAM_NAME)
        creds = LwaCredentials(
            client_id=str(data.get("client_id") or "").strip(),
            client_secret=str(data.get("client_secret") or "").strip(),
            refresh_token=str(data.get("refresh_token") or "").strip(),
        )
    else:
        creds = LwaCredentials(
            client_id=_env("LWA_CLIENT_ID"),
            client_secret=_env("LWA_CLIENT_SECRET"),
            refresh_token=_env("LWA_REFRESH_TOKEN"),
        )
    if not (creds.client_id and creds.client_secret and creds.refresh_token):
        raise RuntimeError("LWA credentials missing client_id/client_secret/refresh_token")
    return creds


@dataclass(frozen=True)
class DbCredentials:
    host: str
    port: str
    dbname: str
    user: str
    password: str


@lru_cache(maxsize=1)
def get_db_credentials() -> DbCredentials:
    if _use_ssm("AMZ_UK_DB_USE_SSM"):
        data = _get_ssm_json(DB_PARAM_NAME)
        creds = DbCredentials(
            host=str(data.get("host") or "").strip(),
            port=str(data.get("port") or "5432").strip(),
            dbname=str(data.get("dbname") or "").strip(),
            user=str(data.get("user") or "").strip(),
            password=str(data.get("password") or "").strip(),
        )
    else:
        creds = DbCredentials(
            host=_env("DB_HOST"),
            port=_env("DB_PORT", "5432"),
            dbname=_env("DB_NAME"),
            user=_env("DB_USER"),
            password=_env("DB_PASSWORD"),
        )
    if not (creds.host and creds.dbname and creds.user and creds.password):
        raise RuntimeError("DB credentials missing host/dbname/user/password")
    return creds


@dataclass(frozen=True)
class SapSlCredentials:
    base_url: str
    companydb: str
    username: str
    password: str


@lru_cache(maxsize=1)
def get_sap_sl_credentials() -> SapSlCredentials:
    if _use_ssm("SAP_SL_USE_SSM"):
        prefix = _sap_sl_ssm_prefix()
        sl = _get_ssm_json(f"{prefix}/sl_config")
        cr = _get_ssm_json(f"{prefix}/credentials")
        creds = SapSlCredentials(
            base_url=str(sl.get("sap_b1_url") or sl.get("SAP_SL_BASE_URL") or "").strip(),
            companydb=str(sl.get("companydb") or sl.get("CompanyDB") or "").strip(),
            username=str(cr.get("username") or cr.get("UserName") or "").strip(),
            password=str(cr.get("password") or cr.get("Password") or "").strip(),
        )
    else:
        creds = SapSlCredentials(
            base_url=_env("SAP_SL_BASE_URL"),
            companydb=_env("SAP_SL_COMPANYDB"),
            username=_env("SAP_SL_USERNAME"),
            password=_env("SAP_SL_PASSWORD"),
        )
    if not (creds.base_url and creds.companydb and creds.username and creds.password):
        raise RuntimeError("SAP SL credentials missing base_url/companydb/username/password")
    return creds

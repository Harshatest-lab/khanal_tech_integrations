"""SAP Service Layer session cache in SSM Parameter Store (shared across Lambdas).

Parameters (SecureString, Standard tier)::

    /sap/sl_config     — JSON: ``sap_b1_url``, ``companydb``
    /sap/credentials   — JSON: ``username``, ``password``
    /sap/b1_session    — JSON: ``b1session``, ``routeid``, ``expires_on``, ``is_locked``

Set env ``SAP_SL_USE_SSM`` to ``1`` / ``true`` / ``yes`` to use SSM + SAP Login in-process on each
caller that imports this package. Otherwise callers can use ``SAP_SL_*`` environment variables only
(see service-specific SAP integration).
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from khanal_shared.logging import log_json
from khanal_shared.sap_sl.http import (
    build_sap_cookie_opener_with_jar,
    inject_sap_sl_cookies,
    sap_sl_request_json,
)

logger = logging.getLogger(__name__)

PARAM_SL_CONFIG = "/sap/sl_config"
PARAM_CREDENTIALS = "/sap/credentials"
PARAM_B1_SESSION = "/sap/b1_session"

_LOCK_WAIT_SEC = 2.0
_MAX_LOCK_WAITS = 30


def sap_sl_use_ssm() -> bool:
    v = (os.environ.get("SAP_SL_USE_SSM") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _ssm() -> Any:
    import boto3

    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()
    if not region:
        region = "us-east-1"
    return boto3.client("ssm", region_name=region)


def _get_param_json(name: str) -> dict[str, Any]:
    from botocore.exceptions import ClientError

    try:
        resp = _ssm().get_parameter(Name=name, WithDecryption=True)
        raw = resp["Parameter"]["Value"]
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        return data
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ParameterNotFound":
            return {}
        raise


def _put_param_json(name: str, data: dict[str, Any]) -> None:
    _ssm().put_parameter(
        Name=name,
        Value=json.dumps(data),
        Type="SecureString",
        Overwrite=True,
    )


def _parse_expires(expires_on_str: str) -> datetime | None:
    if not expires_on_str or not str(expires_on_str).strip():
        return None
    s = str(expires_on_str).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _expires_on_from_timeout_minutes(minutes: int) -> str:
    safe = max(1, min(int(minutes), 120))
    # Renew slightly before SAP idle timeout
    margin = 2 if safe > 3 else 0
    until = datetime.now(timezone.utc) + timedelta(minutes=max(1, safe - margin))
    return until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _host_from_base(base: str) -> str:
    u = base.strip().rstrip("/")
    if not u.startswith("http"):
        u = "https://" + u
    host = urlparse(u).hostname
    if not host:
        raise ValueError(f"Cannot parse hostname from sap_b1_url: {base!r}")
    return host


def _read_sl_and_credentials() -> tuple[str, str, str, str]:
    sl = _get_param_json(PARAM_SL_CONFIG)
    cr = _get_param_json(PARAM_CREDENTIALS)
    base = (sl.get("sap_b1_url") or sl.get("SAP_SL_BASE_URL") or "").strip()
    company = (sl.get("companydb") or sl.get("CompanyDB") or "").strip()
    user = (cr.get("username") or cr.get("UserName") or "").strip()
    pw = (cr.get("password") or cr.get("Password") or "").strip()
    if not base or not company or not user or not pw:
        raise ValueError(
            "SSM /sap/sl_config and /sap/credentials must include "
            "sap_b1_url, companydb, username, password"
        )
    return base.rstrip("/"), company, user, pw


def _jar_cookies_dict(jar: http.cookiejar.CookieJar) -> dict[str, str]:
    """Flatten ``CookieJar`` to name → value (uses jar internal map; stable on CPython)."""
    out: dict[str, str] = {}
    for _domain, paths in jar._cookies.items():
        for _path, cookies in paths.items():
            for _name, c in cookies.items():
                out[_name] = c.value
    return out


def _sap_login_opener(base: str, company: str, user: str, password: str) -> tuple[Any, str, str, int]:
    """Login to Service Layer; return ``(opener, b1session, routeid, session_timeout_min)``."""
    opener, jar = build_sap_cookie_opener_with_jar()
    login_url = f"{base.rstrip('/')}/Login"
    st, body = sap_sl_request_json(
        opener,
        "POST",
        login_url,
        {"CompanyDB": company, "UserName": user, "Password": password},
    )
    if st != 200:
        raise RuntimeError(f"SAP Login HTTP {st}: {body!r}")
    cookies = _jar_cookies_dict(jar)
    b1 = (cookies.get("B1SESSION") or "").strip()
    route = (cookies.get("ROUTEID") or "").strip()
    timeout_min = 30
    if isinstance(body, dict):
        if not b1 and body.get("SessionId"):
            b1 = str(body["SessionId"]).strip()
        st_out = body.get("SessionTimeout")
        try:
            if st_out is not None:
                timeout_min = int(st_out)
        except (TypeError, ValueError):
            timeout_min = 30
    if not b1:
        raise RuntimeError("SAP Login did not return B1SESSION cookie or SessionId")
    return opener, b1, route, timeout_min


def renew_sap_session_ssm() -> tuple[Any, str]:
    """Perform SAP Login using SSM config/credentials and write ``/sap/b1_session`` (caller holds lock)."""
    base, company, user, password = _read_sl_and_credentials()
    _opener, b1, route, timeout_min = _sap_login_opener(base, company, user, password)
    host = _host_from_base(base)
    opener, jar = build_sap_cookie_opener_with_jar()
    inject_sap_sl_cookies(jar, host, b1, route)

    updated = {
        "b1session": b1,
        "routeid": route,
        "expires_on": _expires_on_from_timeout_minutes(timeout_min),
        "is_locked": False,
    }
    _put_param_json(PARAM_B1_SESSION, updated)
    log_json(logger, logging.INFO, "sap_sl_ssm_session_renewed", expires_on=updated["expires_on"])
    return opener, base.rstrip("/")


def _resolve_session_tokens() -> tuple[str, str, str]:
    """Return ``(base_url, b1session, routeid)`` after SSM lock + cache or SAP renew (``base_url`` has no trailing slash)."""
    for _attempt in range(_MAX_LOCK_WAITS):
        session_data = _get_param_json(PARAM_B1_SESSION)
        is_locked = bool(session_data.get("is_locked"))
        b1session_val = (session_data.get("b1session") or "").strip()
        routeid_val = (session_data.get("routeid") or "").strip()
        expires_on_str = session_data.get("expires_on") or ""

        if is_locked:
            log_json(
                logger,
                logging.DEBUG,
                "sap_sl_ssm_wait_lock",
                wait_sec=_LOCK_WAIT_SEC,
            )
            time.sleep(_LOCK_WAIT_SEC)
            continue

        now = datetime.now(timezone.utc)
        exp = _parse_expires(str(expires_on_str))
        is_expired = exp is None or now >= exp

        if not is_expired and b1session_val:
            base, _, _, _ = _read_sl_and_credentials()
            log_json(logger, logging.DEBUG, "sap_sl_ssm_cache_hit", expires_on=expires_on_str)
            return base.rstrip("/"), b1session_val, routeid_val

        try:
            session_data["is_locked"] = True
            _put_param_json(PARAM_B1_SESSION, session_data)
        except Exception:
            log_json(
                logger,
                logging.WARNING,
                "sap_sl_ssm_lock_write_failed",
                trace=traceback.format_exc(),
            )
            time.sleep(_LOCK_WAIT_SEC)
            continue

        try:
            renew_sap_session_ssm()
            refreshed = _get_param_json(PARAM_B1_SESSION)
            base, _, _, _ = _read_sl_and_credentials()
            b1 = (refreshed.get("b1session") or "").strip()
            rt = (refreshed.get("routeid") or "").strip()
            return base.rstrip("/"), b1, rt
        except Exception:
            log_json(
                logger,
                logging.ERROR,
                "sap_sl_ssm_renew_failed",
                trace=traceback.format_exc(),
            )
            try:
                cur = _get_param_json(PARAM_B1_SESSION)
                cur["is_locked"] = False
                _put_param_json(PARAM_B1_SESSION, cur)
            except Exception:
                log_json(
                    logger,
                    logging.WARNING,
                    "sap_sl_ssm_unlock_failed",
                    trace=traceback.format_exc(),
                )
            raise

    raise RuntimeError("SAP SSM session: exceeded max waits while is_locked or contention")


def build_opener_and_base_via_ssm() -> tuple[Any, str]:
    """
    Return ``(urllib_opener, base_url)`` with a valid SAP session, using SSM cache + lock.

    ``base_url`` has no trailing slash (same convention as typical Service Layer callers).
    """
    base, b1, route = _resolve_session_tokens()
    host = _host_from_base(base)
    opener, jar = build_sap_cookie_opener_with_jar()
    inject_sap_sl_cookies(jar, host, b1, route)
    return opener, base

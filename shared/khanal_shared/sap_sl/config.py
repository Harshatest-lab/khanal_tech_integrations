"""SAP Service Layer: detect whether SL is configured and build a session from env vars."""

from __future__ import annotations

import os
from typing import Any

from khanal_shared.sap_sl.http import build_sap_cookie_opener, sap_sl_request_json
from khanal_shared.sap_sl.ssm_session import sap_sl_use_ssm


def sap_sl_enabled() -> bool:
    """True if SSM mode is on **or** classic ``SAP_SL_*`` env credentials are all set."""
    if sap_sl_use_ssm():
        return True
    base = (os.environ.get("SAP_SL_BASE_URL") or "").strip()
    company = (os.environ.get("SAP_SL_COMPANYDB") or "").strip()
    user = (os.environ.get("SAP_SL_USERNAME") or "").strip()
    pw = (os.environ.get("SAP_SL_PASSWORD") or "").strip()
    return bool(base and company and user and pw)


def build_opener_and_base_from_env() -> tuple[Any, str, int]:
    """
    ``POST …/Login`` using ``SAP_SL_BASE_URL``, ``SAP_SL_COMPANYDB``, ``SAP_SL_USERNAME``, ``SAP_SL_PASSWORD``.

    Returns ``(opener, base_url, login_http_status)`` where ``base_url`` has no trailing slash.
    ``opener`` has session cookies when ``login_http_status == 200``.
    """
    base = os.environ["SAP_SL_BASE_URL"].strip().rstrip("/")
    company = os.environ["SAP_SL_COMPANYDB"].strip()
    user = os.environ["SAP_SL_USERNAME"].strip()
    password = os.environ["SAP_SL_PASSWORD"].strip()

    opener = build_sap_cookie_opener()
    login_url = f"{base}/Login"
    st_login, _ = sap_sl_request_json(
        opener,
        "POST",
        login_url,
        {"CompanyDB": company, "UserName": user, "Password": password},
    )
    return opener, base, st_login

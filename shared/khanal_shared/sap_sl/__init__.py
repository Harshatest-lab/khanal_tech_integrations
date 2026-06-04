"""SAP Business One Service Layer helpers (urllib HTTP + optional SSM session cache)."""

from khanal_shared.sap_sl.config import build_opener_and_base_from_env, sap_sl_enabled
from khanal_shared.sap_sl.http import (
    build_sap_cookie_opener,
    build_sap_cookie_opener_with_jar,
    inject_sap_sl_cookies,
    sap_sl_request_json,
)
from khanal_shared.sap_sl.ssm_session import build_opener_and_base_via_ssm, sap_sl_use_ssm

__all__ = [
    "build_opener_and_base_from_env",
    "build_opener_and_base_via_ssm",
    "build_sap_cookie_opener",
    "build_sap_cookie_opener_with_jar",
    "inject_sap_sl_cookies",
    "sap_sl_enabled",
    "sap_sl_request_json",
    "sap_sl_use_ssm",
]

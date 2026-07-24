"""Unicommerce posting settings: replaces the Frappe "Unicommerce Settings"
Single doctype with one SSM SecureString JSON parameter, cached in-process
for the life of the Lambda container -- same pattern as credentials.py.

Expected JSON shape (all keys optional except noted; see the Frappe source
these were ported from for the exact semantics each map drives):

{
  "channel_customer_map": {"<channel>": "<SAP CardCode>", "default": "..."},
  "tax_inclusive_map": {"<channel>": true},
  "tax_code_map": {"<pct>_<intra|inter>": "<SAP TaxCode>", "0_intra": "...", "0_exempt": "..."},
  "warehouse_map": {"<uc_warehouse_code>": "<sap_warehouse_code>"},
  "channel_warehouse_map": {"<channel>": "<sap_warehouse_code>"},
  "bill_to_code_map": {"<channel>:<igst|cgst_sgst>": "<code>"},
  "ship_to_code_map": {"<channel>:<igst|cgst_sgst>": "<code>"},
  "gl_account_map": {"<channel>": {"<state>": {"intra"|"inter": {"product"|"shipping": "<AcctCode>"}}}},
  "channel_origin_state_map": {"<channel>": "<state code>"},
  "origin_state_code": "KA",
  "ar_invoice_series_map": {"<channel>": <series id>},
  "ar_invoice_series": <series id>,           -- fallback if channel not in the map
  "credit_note_series": <series id>,
  "max_retry_attempts": 8,
  "posting_timeout_mins": 5,
  "ec_fg_fefo_fallback": false,
  "vendor_code": "EC-DIS"
}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


UC_SETTINGS_PARAM_NAME = _env("UC_SETTINGS_PARAM_NAME", "/unicommerce/settings")


@dataclass(frozen=True)
class UnicommerceSettings:
    channel_customer_map: dict = field(default_factory=dict)
    tax_inclusive_map: dict = field(default_factory=dict)
    tax_code_map: dict = field(default_factory=dict)
    warehouse_map: dict = field(default_factory=dict)
    channel_warehouse_map: dict = field(default_factory=dict)
    bill_to_code_map: dict = field(default_factory=dict)
    ship_to_code_map: dict = field(default_factory=dict)
    gl_account_map: dict = field(default_factory=dict)
    channel_origin_state_map: dict = field(default_factory=dict)
    origin_state_code: str = "KA"
    ar_invoice_series_map: dict = field(default_factory=dict)
    ar_invoice_series: int | None = None
    credit_note_series: int | None = None
    max_retry_attempts: int = 8
    posting_timeout_mins: int = 5
    ec_fg_fefo_fallback: bool = False
    vendor_code: str = "EC-DIS"


_settings_cache: UnicommerceSettings | None = None


def get_settings(force_reload: bool = False) -> UnicommerceSettings:
    global _settings_cache
    if _settings_cache is not None and not force_reload:
        return _settings_cache

    local = _settings_from_local()
    if local is not None:
        _settings_cache = local
        return _settings_cache

    import boto3

    ssm = boto3.client("ssm")
    raw = ssm.get_parameter(Name=UC_SETTINGS_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(raw)
    _settings_cache = _from_dict(data)
    return _settings_cache


def _from_dict(data: dict) -> UnicommerceSettings:
    return UnicommerceSettings(
        channel_customer_map=data.get("channel_customer_map") or {},
        tax_inclusive_map=data.get("tax_inclusive_map") or {},
        tax_code_map=data.get("tax_code_map") or {},
        warehouse_map=data.get("warehouse_map") or {},
        channel_warehouse_map=data.get("channel_warehouse_map") or {},
        bill_to_code_map=data.get("bill_to_code_map") or {},
        ship_to_code_map=data.get("ship_to_code_map") or {},
        gl_account_map=data.get("gl_account_map") or {},
        channel_origin_state_map=data.get("channel_origin_state_map") or {},
        origin_state_code=data.get("origin_state_code") or "KA",
        ar_invoice_series_map=data.get("ar_invoice_series_map") or {},
        ar_invoice_series=data.get("ar_invoice_series"),
        credit_note_series=data.get("credit_note_series"),
        max_retry_attempts=int(data.get("max_retry_attempts") or 8),
        posting_timeout_mins=int(data.get("posting_timeout_mins") or 5),
        ec_fg_fefo_fallback=bool(data.get("ec_fg_fefo_fallback") or False),
        vendor_code=data.get("vendor_code") or "EC-DIS",
    )


def _settings_from_local() -> UnicommerceSettings | None:
    try:
        from unicommerce.credentials_local import UC_SETTINGS  # type: ignore[import-not-found]
    except ImportError:
        return None
    return _from_dict(UC_SETTINGS)

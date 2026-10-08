"""State/tax-bucket resolution. Ported from
khanal_tech_integrations/unicommerce/geo.py -- pure logic except
resolve_origin_state_code, which took settings from frappe.get_single()
there; here it's a required argument (unicommerce.settings.UnicommerceSettings)."""

from __future__ import annotations

from typing import Any

from unicommerce.settings import UnicommerceSettings

DEFAULT_ORIGIN_STATE_CODE = "KA"

# Fallback when settings.channel_origin_state_map doesn't list the channel.
# Matches Uniware channel_name values (e.g. AMAZON_FBA_IN_*). All other
# channels use settings.origin_state_code (typically KA).
DEFAULT_CHANNEL_ORIGIN_STATE_OVERRIDES = {
    "AMAZON_FBA_IN_MAA4": "TN",
    "AMAZON_FBA_IN_CJB1": "TN",
    "AMAZON_FBA_IN_DEL4": "HR",
    "AMAZON_FBA_IN_DEL5": "HR",
    "AMAZON_FBA_IN_BOM5": "MH",
    "AMAZON_FBA_IN_BOM7": "MH",
    "AMAZON_FBA_IN_PNQ3": "MH",
    "FLIPKART_FA_MH_BHIWANDI": "MH",
    "FLIPKART_FA_HR_SANPKA": "HR",
}

STATE_ALIASES = {
    "KA": "KA", "KARNATAKA": "KA",
    "TN": "TN", "TAMIL NADU": "TN",
    "MH": "MH", "MAHARASHTRA": "MH",
    "DL": "DL", "DELHI": "DL",
    "HR": "HR", "HARYANA": "HR",
    "TG": "TG", "TELANGANA": "TG", "TS": "TG",
    "AP": "AP", "ANDHRA PRADESH": "AP",
    "KL": "KL", "KERALA": "KL",
    "WB": "WB", "WEST BENGAL": "WB",
    "GJ": "GJ", "GUJARAT": "GJ",
    "RJ": "RJ", "RAJASTHAN": "RJ",
    "UP": "UP", "UTTAR PRADESH": "UP",
    "MP": "MP", "MADHYA PRADESH": "MP",
    "PB": "PB", "PUNJAB": "PB",
    "CH": "CH", "CHANDIGARH": "CH",
    "OR": "OD", "OD": "OD", "ODISHA": "OD", "ORISSA": "OD",
    "JH": "JH", "JHARKHAND": "JH",
    "BR": "BR", "BIHAR": "BR",
    "AS": "AS", "ASSAM": "AS",
    "GA": "GA", "GOA": "GA",
    "JK": "JK", "JAMMU AND KASHMIR": "JK",
    "UTTARAKHAND": "UK", "UK": "UK",
    "HP": "HP", "HIMACHAL PRADESH": "HP",
    "CHHATTISGARH": "CG", "CG": "CG",
}

STATE_REGION_MAP = {
    "KA": "South", "TN": "South", "KL": "South", "AP": "South", "TG": "South",
    "MH": "West", "GJ": "West", "GA": "West",
    "RJ": "North", "DL": "North", "HR": "North", "PB": "North", "CH": "North",
    "JK": "North", "HP": "North", "UK": "North",
    "UP": "Central", "MP": "Central", "CG": "Central",
    "WB": "East", "OD": "East", "JH": "East", "BR": "East",
    "AS": "North East",
}


def resolve_mapped_value(mapping: dict, key: str | None, default: Any = None) -> Any:
    normalized_key = str(key or "").strip()
    if not mapping:
        return default
    if normalized_key in mapping:
        return mapping[normalized_key]
    key_upper = normalized_key.upper()
    for candidate, value in mapping.items():
        if str(candidate).strip().upper() == key_upper:
            return value
    return default


def normalize_state_code(raw_state: Any) -> str:
    state = str(raw_state or "").strip().upper().replace(".", " ").replace("-", " ")
    state = " ".join(part for part in state.split() if part)
    return STATE_ALIASES.get(state, state[:2] if len(state) == 2 else state)


def extract_shipping_state(uc_data: dict) -> str:
    for address in (
        uc_data.get("shippingAddress") or {},
        uc_data.get("billingAddress") or {},
        ((uc_data.get("addresses") or [{}])[0] or {}),
    ):
        state = normalize_state_code(address.get("state"))
        if state:
            return state
    return ""


def resolve_origin_state_code(channel: str | None, settings: UnicommerceSettings) -> str:
    resolved = resolve_mapped_value(settings.channel_origin_state_map, channel)
    if not resolved:
        resolved = resolve_mapped_value(DEFAULT_CHANNEL_ORIGIN_STATE_OVERRIDES, channel)
    if not resolved:
        resolved = settings.origin_state_code
    normalized = normalize_state_code(resolved)
    return normalized or DEFAULT_ORIGIN_STATE_CODE


def classify_tax_bucket(
    ship_state_code: str,
    payload_json: Any = None,
    origin_state_code: str = DEFAULT_ORIGIN_STATE_CODE,
) -> str:
    normalized_ship_state = normalize_state_code(ship_state_code)
    normalized_origin = normalize_state_code(origin_state_code) or DEFAULT_ORIGIN_STATE_CODE

    if normalized_ship_state:
        return "cgst_sgst" if normalized_ship_state == normalized_origin else "igst"

    payload_upper = str(payload_json or "").upper()
    if "IGST" in payload_upper or "KAIG" in payload_upper:
        return "igst"
    if "CGST" in payload_upper or "SGST" in payload_upper or "KACS" in payload_upper:
        return "cgst_sgst"
    return "unknown"

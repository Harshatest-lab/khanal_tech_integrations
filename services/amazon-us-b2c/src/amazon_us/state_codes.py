"""USPS state-code normalization for the U_State line UDF.

Amazon's ship-state is free-form buyer input ("CA", "California",
"california", "N.Y.", ...); SAP-side reporting can only group by state
when the value is the USPS two-letter code. Ported verbatim from the
Frappe pipeline (mappers/payload.py).
"""

from __future__ import annotations

_US_STATE_CODES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC", "WASHINGTON DC": "DC", "WASHINGTON D C": "DC",
    "PUERTO RICO": "PR", "VIRGIN ISLANDS": "VI", "US VIRGIN ISLANDS": "VI",
    "GUAM": "GU", "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP",
    "ARMED FORCES AMERICAS": "AA", "ARMED FORCES EUROPE": "AE", "ARMED FORCES PACIFIC": "AP",
}
_VALID_STATE_CODES = set(_US_STATE_CODES.values())


def normalize_us_state_code(value: str | None) -> str | None:
    """"California" / "california" / "CA" / "ca" / "N.Y." -> "CA"/"NY".
    Unrecognized values (foreign regions, typos) are returned as entered so
    nothing is silently dropped — SAP still gets whatever Amazon sent."""
    if not value:
        return value
    raw = str(value).strip()
    cleaned = " ".join(raw.upper().replace(".", " ").split())
    if cleaned in _VALID_STATE_CODES:
        return cleaned
    code = _US_STATE_CODES.get(cleaned)
    if code:
        return code
    compact = cleaned.replace(" ", "")
    if compact in _VALID_STATE_CODES:
        return compact
    return raw

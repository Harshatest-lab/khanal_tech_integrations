"""GST extraction from PO documents and validation against SAP (HANA CRD1/OCRD)."""

from __future__ import annotations

import re
from typing import Any

_GSTIN_BODY = r"[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}"
# Blinkit: GST No.: / GSTIN:
# Bigbasket: GSTIN NO: / GSTIN NO:GST:
_GSTIN_LABEL = r"(?:GST\s*No\.?|GSTIN(?:\s*NO\.?)?)\s*:?\s*(?:GST\s*:?\s*)?"


def normalize_gstin(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).upper().strip()


def extract_gst_from_text(text: str) -> str:
    """Extract ship-to / warehouse GST from PO PDF or Excel text (all channels)."""
    if not text:
        return "Not Found"
    block_patterns = (
        rf"Shipping\s+Address[\s\S]{{0,1200}}?{_GSTIN_LABEL}({_GSTIN_BODY})",
        rf"Delivered\s*To\s*:[\s\S]{{0,1200}}?{_GSTIN_LABEL}({_GSTIN_BODY})",
        rf"Warehouse\s+Address[\s\S]{{0,1200}}?{_GSTIN_LABEL}({_GSTIN_BODY})",
        rf"Delivery\s+Address[\s\S]{{0,1200}}?{_GSTIN_LABEL}({_GSTIN_BODY})",
    )
    for pattern in block_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return normalize_gstin(match.group(1))
    labeled = re.findall(
        rf"{_GSTIN_LABEL}({_GSTIN_BODY})",
        text,
        flags=re.IGNORECASE,
    )
    if labeled:
        pick = labeled[1] if len(labeled) >= 2 else labeled[0]
        return normalize_gstin(pick)
    return "Not Found"


def sap_gstin(sap_addr: dict[str, Any] | None, bp: dict[str, Any] | None) -> str:
    addr = sap_addr or {}
    partner = bp or {}
    return normalize_gstin(
        addr.get("GSTIN") or addr.get("FederalTaxID") or partner.get("GSTIN") or partner.get("FederalTaxID")
    )


def validate_gst_match(extracted_gst: str, sap_addr: dict[str, Any] | None, bp: dict[str, Any] | None) -> str | None:
    """Return error message when extracted GST does not match SAP, else None."""
    extracted = normalize_gstin(extracted_gst)
    sap_gst = sap_gstin(sap_addr, bp)
    if not extracted or not sap_gst or extracted != sap_gst:
        return f"GST mismatch. Extracted: {extracted or 'Not Found'}, SAP: {sap_gst or 'Not Found'}."
    return None

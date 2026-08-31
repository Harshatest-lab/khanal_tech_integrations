"""Zepto PO intake: PDF header (PO No + shipping address) + CSV line items (Frappe Zepto.py).

PO expiry: CSV ``PoExpiryDate`` first; PDF ``PO Expiry Date`` only when CSV is missing.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from difflib import get_close_matches
from typing import Any

from gst_validate import extract_gst_from_text

_REQUIRED_COLUMNS = [
    "PoNumber",
    "Sku",
    "SkuDesc",
    "IGSTPercentage",
    "MRP",
    "Quantity",
    "UnitBaseCost",
    "LandingCost",
    "TotalAmount",
    "PoExpiryDate",
]


def extract_pdf_text(pdf_bytes: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            piece = page.extract_text()
            if piece:
                parts.append(piece)
    return "\n".join(parts)


def is_valid_zepto_pdf_text(text: str) -> bool:
    has_po = bool(re.search(r"\bPO\s*No\s*:", text, re.I))
    has_ship = "shipping address" in (text or "").lower()
    return has_po and has_ship


def _parse_expiry(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    if not cleaned:
        return "Not Found"
    # Zepto CSV often sends "2026-09-02 00:00:00" (Frappe Zepto.py formats).
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d/%m/%y %H:%M",
        "%d-%m-%Y",
        "%d/%b/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Trim time portion when strptime formats differ slightly.
    if re.match(r"^\d{4}-\d{2}-\d{2}\b", cleaned):
        return cleaned[:10]
    return "Invalid Date Format"


def _extract_po_expiry_from_text(text: str) -> str:
    match = re.search(
        r"PO\s+Expiry\s+Date\s*:\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[/-][0-9A-Za-z]+[/-][0-9]{2,4})",
        text or "",
        re.I,
    )
    if not match:
        return "Not Found"
    return _parse_expiry(match.group(1))


def _valid_expiry(expiry: str) -> bool:
    return bool(expiry and expiry not in {"", "Not Found", "Invalid Date Format"})


def resolve_po_expiry(parsed: dict[str, Any] | None, pdf_text: str | None = None) -> str:
    """CSV PoExpiryDate first (Frappe); PDF PO Expiry Date as fallback."""
    csv_expiry = _parse_expiry(str((parsed or {}).get("po_expiry_date") or ""))
    if _valid_expiry(csv_expiry):
        return csv_expiry
    pdf_expiry = _extract_po_expiry_from_text(pdf_text or "")
    if _valid_expiry(pdf_expiry):
        return pdf_expiry
    return "Not Found"


def extract_header_from_text(text: str) -> dict[str, str]:
    po_number = "Not Found"
    po_match = re.search(r"\bPO\s*No\s*:\s*(\w+)", text or "", re.I)
    if po_match:
        po_number = po_match.group(1)

    address_lines: list[str] = []
    lines = (text or "").split("\n")
    try:
        index = next(i for i, line in enumerate(lines) if "shipping address" in line.lower())
        for i in range(index + 1, len(lines)):
            line = lines[i].strip()
            if not line:
                continue
            match = re.search(r"\b\d{6}\b", line)
            if match:
                address_lines.append(line[: match.end()])
                break
            if line not in address_lines:
                address_lines.append(line)
    except StopIteration:
        pass

    address_lines = [
        line.replace("Shipping Address:", "").replace("Address:", "").strip()
        for line in address_lines
        if line
    ]
    delivered_to = "\n".join(address_lines) if address_lines else ""
    gst_no = extract_gst_from_text(text or "")
    return {
        "company_name": "Zepto",
        "contact_name": "Not Found",
        "po_number": po_number,
        "po_expiry_date": "Not Found",
        "delivered_to": delivered_to,
        "gst_no": gst_no,
    }


def extract_header(pdf_bytes: bytes) -> tuple[dict[str, str], str]:
    text = extract_pdf_text(pdf_bytes)
    if not is_valid_zepto_pdf_text(text):
        raise ValueError("Not a Zepto PO PDF (missing PO No or Shipping Address)")
    return extract_header_from_text(text), text


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def parse_csv(csv_bytes: bytes) -> dict[str, Any]:
    csv_content = csv_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(csv_content))
    headers = reader.fieldnames or []
    lower_headers = [h.lower() for h in headers]
    column_mapping: dict[str, str] = {}
    for required in _REQUIRED_COLUMNS:
        match = get_close_matches(required.lower(), lower_headers, n=1, cutoff=0.6)
        if match:
            column_mapping[required] = next(h for h in headers if h.lower() == match[0])

    items: list[dict[str, Any]] = []
    po_numbers: set[str] = set()
    expiry = ""
    for row in reader:
        extracted = {key: row.get(column_mapping[key], "") for key in column_mapping}
        po = str(extracted.get("PoNumber") or "").strip()
        if po:
            po_numbers.add(po)
        if not expiry:
            expiry = str(extracted.get("PoExpiryDate") or "").strip()
        items.append(extracted)
    return {"items": items, "po_numbers": po_numbers, "po_expiry_date": expiry}


def lines_from_csv_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        sku = str(item.get("Sku") or "").strip()
        if not sku:
            continue
        rows.append(
            {
                "item_code": sku,
                "quantity": _to_int(item.get("Quantity")),
                "discount": 0,
                "product_description": str(item.get("SkuDesc") or ""),
                "basic_cost_price": _to_float(item.get("UnitBaseCost")),
                "igst_percentage": _to_float(item.get("IGSTPercentage")),
                "tax_amount": 0,
                "landing_rate": _to_float(item.get("LandingCost")),
                "mrp": _to_float(item.get("MRP")),
                "total_amount": _to_float(item.get("TotalAmount")),
            }
        )
    return rows


def parse_attachment(csv_bytes: bytes) -> dict[str, Any]:
    parsed = parse_csv(csv_bytes)
    return {
        **parsed,
        "lines": lines_from_csv_items(parsed.get("items") or []),
        "po_expiry_date": _parse_expiry(str(parsed.get("po_expiry_date") or "")),
    }


def apply_csv_to_header(
    header: dict[str, str],
    parsed: dict[str, Any],
    pdf_text: str | None = None,
) -> None:
    """Set Zepto PO expiry: CSV first, PDF fallback."""
    header["po_expiry_date"] = resolve_po_expiry(parsed, pdf_text)

"""Required-field validation after PO documents are parsed (all B2B channels)."""

from __future__ import annotations

from typing import Any

_MISSING = {"", "Not Found", "Unknown", "Invalid Date Format", "N/A", "Not Available"}


def _is_missing(value: Any) -> bool:
    return str(value or "").strip() in _MISSING


def _doc_source(channel: str) -> str:
    ch = (channel or "").strip().lower()
    if ch == "bigbasket":
        return "the Excel file"
    if ch == "zepto":
        return "the PDF or CSV"
    if ch == "instamart":
        return "the PDF or Excel file"
    return "the PDF"


def _usable_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in lines if str(row.get("item_code") or "").strip()]


def zepto_pdf_csv_po_mismatch(pdf_po: str, csv_po_numbers: set[str] | list[str] | None) -> str | None:
    """Frappe Zepto.py: PDF PO number must appear in CSV PoNumber column."""
    po = str(pdf_po or "").strip()
    numbers = {str(p).strip() for p in (csv_po_numbers or []) if str(p).strip()}
    if _is_missing(po) or not numbers:
        return None
    if po not in numbers:
        shown = ", ".join(sorted(numbers))
        return f"PO number mismatch: PDF has {po} but CSV has {shown}."
    return None


def validate_intake_data(
    channel: str,
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    skip_lines: bool = False,
) -> list[str]:
    """Return blocking errors when required header/line data could not be read."""
    ch = (channel or "").strip().lower()
    src = _doc_source(ch)
    errors: list[str] = []

    if _is_missing(header.get("po_expiry_date")):
        errors.append(f"PO expiry date (SAP Doc due date) could not be read from {src}.")

    delivered = str(header.get("delivered_to") or "").strip()
    if not delivered or delivered in _MISSING:
        errors.append(f"Ship-to / delivery address could not be read from {src}.")

    if _is_missing(header.get("gst_no")):
        errors.append(f"GST number could not be read from {src}.")

    if skip_lines:
        return errors

    usable = _usable_lines(lines)
    if not usable:
        if ch == "zepto":
            errors.append("No line items could be read from the CSV attachment.")
        elif ch == "bigbasket":
            errors.append("No line items could be read from the Excel file.")
        elif ch == "instamart":
            errors.append("No line items could be read from the Excel attachment.")
        else:
            errors.append("No line items could be read from the Excel attachment.")
    elif all(float(row.get("quantity") or 0) <= 0 for row in usable):
        errors.append("Line items were found but all quantities are zero.")

    return errors


def join_errors(*parts: str) -> str:
    bits: list[str] = []
    seen: set[str] = set()
    for part in parts:
        for piece in str(part or "").split("."):
            text = piece.strip()
            if not text:
                continue
            if not text.endswith("."):
                text = text + "."
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            bits.append(text)
    return " ".join(bits).strip()

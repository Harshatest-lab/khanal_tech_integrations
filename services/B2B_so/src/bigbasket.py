"""Bigbasket PO intake: Excel (.xls/.xlsx/HTML) header + line items (Frappe Bigbasket.py)."""

from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any

from gst_validate import extract_gst_from_text

_SNO_RE = re.compile(r"\b(s(\.?|l)?\.?\s?no)\b", re.I)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _norm_col(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", _clean_text(value).lower())


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


def _parse_expiry(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", (raw or "").strip())
    if not cleaned:
        return "Not Found"
    for fmt in ("%d/%b/%Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "Invalid Date Format"


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return _clean_text(text)


def _rows_from_html(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", text):
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)
        cleaned = [_html_to_text(cell) for cell in cells]
        if any(cleaned):
            rows.append(cleaned)
    return rows


def _rows_from_openpyxl(excel_bytes: bytes) -> list[list[str]]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(excel_bytes), data_only=True)
    sheet = workbook.active
    if sheet is None:
        return []
    rows: list[list[str]] = []
    for row in sheet.iter_rows(values_only=True):
        cells = [_clean_text(cell) for cell in row]
        if any(cells):
            rows.append(cells)
    return rows


def _load_sheets(excel_bytes: bytes, filename: str = "") -> list[list[list[str]]]:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or excel_bytes[:2] == b"PK":
        return [_rows_from_openpyxl(excel_bytes)]
    text = excel_bytes.decode("utf-8", errors="replace")
    html_rows = _rows_from_html(text)
    if html_rows:
        return [html_rows]
    if "<Workbook" in text or "ss:Workbook" in text:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(text)
        xml_rows: list[list[str]] = []
        for row in root.findall(".//{*}Row") or root.findall(".//Row"):
            cells = []
            for cell in row.findall("{*}Cell") or row.findall("Cell"):
                data = cell.find("{*}Data")
                if data is None:
                    data = cell.find("Data")
                cells.append(_clean_text("".join((data if data is not None else cell).itertext())))
            if any(cells):
                xml_rows.append(cells)
        if xml_rows:
            return [xml_rows]
    raise ValueError("Could not read Bigbasket Excel (.xls/.xlsx)")


def _col_index(header: list[str], *names: str) -> int | None:
    wanted = {_norm_col(name) for name in names}
    for idx, cell in enumerate(header):
        if _norm_col(cell) in wanted:
            return idx
    return None


def extract_from_rows(rows: list[list[str]]) -> dict[str, Any]:
    po_number = "Not Found"
    expiry = "Not Found"
    warehouse: list[str] = []

    for idx, row in enumerate(rows):
        joined = " ".join(row)
        if po_number == "Not Found":
            po_match = re.search(r"PO Number[:\s]*(\S+)", joined, re.I)
            if po_match:
                po_number = po_match.group(1).strip()
        if expiry == "Not Found":
            exp_match = re.search(r"PO Expiry date[:\s]*([0-9A-Za-z/\-]+)", joined, re.I)
            if exp_match:
                expiry = _parse_expiry(exp_match.group(1))
        if "warehouse address" in joined.lower() and not warehouse:
            # Frappe Bigbasket.py: warehouse block is column A only; stop at pincode (not GSTIN).
            for follow in rows[idx : idx + 15]:
                line = _clean_text(follow[0]) if follow else ""
                if not line:
                    if warehouse:
                        break
                    continue
                low = line.lower()
                if _SNO_RE.search(line) or low.startswith("po number"):
                    break
                warehouse.append(line)
                if re.search(r"\b\d{6}\b", line):
                    break

    header_idx = None
    for idx, row in enumerate(rows):
        joined = " ".join(row)
        if _SNO_RE.search(joined):
            header_idx = idx
            break

    items: list[dict[str, Any]] = []
    if header_idx is not None:
        header = rows[header_idx]
        sku_i = _col_index(header, "SkuCode", "SKU Code", "SKU")
        qty_i = _col_index(header, "Quantity", "Qty")
        desc_i = _col_index(header, "Description", "SKU Description", "Item Description")
        basic_i = _col_index(header, "Basic Cost", "BasicCost")
        land_i = _col_index(header, "Landing Cost", "LandingCost")
        mrp_i = _col_index(header, "MRP")
        total_i = _col_index(header, "Total Value", "TotalValue", "Total Amount")
        gst_i = _col_index(header, "GST Amount", "GSTAmount", "IGST")

        def at(row: list[str], col: int | None) -> str:
            if col is None or col >= len(row):
                return ""
            return row[col]

        for row in rows[header_idx + 1 :]:
            joined = " ".join(row)
            if re.search(r"\btotal\b", joined, re.I) and not at(row, sku_i):
                break
            sku = at(row, sku_i)
            if not sku:
                continue
            items.append(
                {
                    "item_code": sku,
                    "quantity": _to_int(at(row, qty_i)),
                    "discount": 0,
                    "product_description": at(row, desc_i),
                    "basic_cost_price": _to_float(at(row, basic_i)),
                    "igst_percentage": 0,
                    "tax_amount": _to_float(at(row, gst_i)),
                    "landing_rate": _to_float(at(row, land_i)),
                    "mrp": _to_float(at(row, mrp_i)),
                    "total_amount": _to_float(at(row, total_i)),
                }
            )

    delivered_to = "\n".join(warehouse)
    sheet_text = "\n".join(" ".join(row) for row in rows)
    gst_no = extract_gst_from_text(f"{delivered_to}\n{sheet_text}")
    if po_number in {"Not Found", ""}:
        raise ValueError("Not a Bigbasket PO Excel (missing PO Number)")
    return {
        "company_name": "Bigbasket",
        "contact_name": "Not Found",
        "po_number": po_number,
        "po_expiry_date": expiry,
        "delivered_to": delivered_to,
        "gst_no": gst_no,
        "lines": items,
        "Warehouse Address": delivered_to,
    }


def parse_excel_attachment(excel_bytes: bytes, filename: str = "") -> dict[str, Any]:
    for rows in _load_sheets(excel_bytes, filename):
        try:
            return extract_from_rows(rows)
        except ValueError:
            continue
    raise ValueError("Not a Bigbasket PO Excel (missing PO Number)")


def header_from_parsed(parsed: dict[str, Any]) -> dict[str, str]:
    return {
        "company_name": str(parsed.get("company_name") or "Bigbasket"),
        "contact_name": str(parsed.get("contact_name") or "Not Found"),
        "po_number": str(parsed.get("po_number") or "Not Found"),
        "po_expiry_date": str(parsed.get("po_expiry_date") or "Not Found"),
        "delivered_to": str(parsed.get("delivered_to") or parsed.get("Warehouse Address") or ""),
        "gst_no": str(parsed.get("gst_no") or "Not Found"),
    }

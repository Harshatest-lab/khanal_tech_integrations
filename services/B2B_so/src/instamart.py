"""Instamart PO intake: PDF header + Excel (.xls/.xlsx) line items (Frappe Instamart.py)."""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any

from gst_validate import extract_gst_from_text

_INSTAMART_COMPANY = "SCOOTSY LOGISTICS PRIVATE LIMITED"


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\t", " ")
    return re.sub(r"[ ]{2,}", " ", text)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            piece = page.extract_text()
            if piece:
                parts.append(piece)
    return normalize_text("\n".join(parts))


def is_valid_instamart_pdf_text(text: str) -> bool:
    text = normalize_text(text)
    has_company = bool(re.search(r"SCOOTSY\s+LOGISTICS\s+PRIVATE\s+LIMITED", text, re.I))
    has_po = bool(re.search(r"PO\s+No", text, re.I))
    return has_company and has_po


def _parse_expiry(raw: str) -> str:
    cleaned = re.sub(r"\s+", " ", raw.strip())
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "Invalid Date Format"


def _normalize_excel_date(raw: str) -> str:
    """Normalize SpreadsheetML / openpyxl date cells to YYYY-MM-DD."""
    value = _clean_text(str(raw or ""))
    if not value:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    parsed = _parse_expiry(value)
    return parsed if parsed != "Invalid Date Format" else value


def extract_delivered_to_from_text(text: str) -> str:
    """Shipping address from Instamart PDF text (billing + shipping columns flatten to two blocks)."""
    if not text:
        return ""
    raw = text.replace("\t", " ")
    section = ""
    for pattern in (
        r"Billing\s+Address\s+Shipping\s+Address\s*(.*?)(?=S\.\s*\n?\s*No\b|S\.\s+No\b|Item\s+Code\b|\b1\s+\d{5,}\b)",
        r"Shipping\s+Address\s*(.*?)(?=S\.\s*\n?\s*No\b|S\.\s+No\b|Item\s+Code\b)",
    ):
        match = re.search(pattern, raw, re.I | re.DOTALL)
        if match:
            section = match.group(1)
            break
    if not section:
        return ""

    sco_blocks = re.findall(
        r"(SCOOTSY\s+LOGISTICS\s+PRIVATE\s+LIMITED[\s\S]*?)(?=SCOOTSY\s+LOGISTICS\s+PRIVATE\s+LIMITED|ggnpod|\Z)",
        section,
        re.I,
    )
    block = sco_blocks[-1] if sco_blocks else section

    lines: list[str] = []
    for line in re.split(r"[\n\r]+", block):
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue
        if re.search(r"^(Contact\s*:|GSTIN\s*:|PAN\s*:|ggnpod|[^\s]+@)", line, re.I):
            break
        lines.append(line)
    shipping_clean = "\n".join(lines)
    if not shipping_clean:
        return ""

    main_match = re.search(
        r"SCOOTSY\s+LOGISTICS\s+PRIVATE\s+LIMITED.*?(?:\s-\s|,\s*)\d{6}",
        shipping_clean,
        re.I | re.DOTALL,
    )
    if main_match:
        return re.sub(r"\s+", " ", main_match.group(0)).strip()
    return shipping_clean


def extract_header_from_text(text: str) -> dict[str, str]:
    text = normalize_text(text)
    data: dict[str, str] = {}
    company_match = re.search(r"SCOOTSY\s+LOGISTICS\s+PRIVATE\s+LIMITED", text, re.I)
    data["company_name"] = _INSTAMART_COMPANY if company_match else "Not Found"
    po_match = re.search(r"PO\s+No\s*:\s*([A-Za-z0-9]+)", text, re.I)
    data["po_number"] = po_match.group(1) if po_match else "Not Found"
    expiry_match = re.search(
        r"PO\s+Expiry\s+Date\s*[:\-]?\s*([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
        text,
        re.I,
    )
    data["po_expiry_date"] = _parse_expiry(expiry_match.group(1)) if expiry_match else "Not Found"
    data["contact_name"] = "Not Found"
    data["delivered_to"] = extract_delivered_to_from_text(text)
    data["gst_no"] = extract_gst_from_text(text)
    return data


def extract_header(pdf_bytes: bytes) -> tuple[dict[str, str], str]:
    text = extract_pdf_text(pdf_bytes)
    if not is_valid_instamart_pdf_text(text):
        raise ValueError("Not an Instamart PO PDF (missing required markers)")
    return extract_header_from_text(text), text


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip() if value else ""


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


def _cell_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return _clean_text("".join(node.itertext()))


def _row_cells(row: ET.Element) -> list[str]:
    """Read SpreadsheetML row values (Data lives inside Cell, not as direct Row children)."""
    cells = [_cell_text(cell) for cell in row.findall("{*}Cell") or row.findall("Cell")]
    if cells:
        return cells
    return [_cell_text(node) for node in row.findall(".//{*}Data") or row.findall(".//Data")]


def read_xml_excel(xml_data: str) -> dict[str, Any]:
    root = ET.fromstring(xml_data)
    rows = root.findall(".//{*}Row")
    if not rows:
        rows = root.findall(".//Row")

    def process_address(lines: list[str]) -> str:
        return "\n".join(line.strip() for line in lines if line.strip())

    po_details: dict[str, str] = {}
    po_number_found = False

    for row in rows:
        cells = _row_cells(row)
        if not cells:
            continue

        for idx, cell in enumerate(cells):
            if "PO No" in cell or "PO Number" in cell:
                if idx + 1 < len(cells):
                    po_details["PO No"] = _clean_text(cells[idx + 1])
                else:
                    po_details["PO No"] = _clean_text(cell.split(":")[-1])
                po_number_found = True

        if cells and "Vendor Name" in cells[0] and len(cells) >= 2:
            vendor_val = _clean_text(cells[1])
            if vendor_val and "PO No" not in vendor_val:
                po_details["Vendor Name"] = vendor_val
        elif cells and "PO Date :" in cells[0] and len(cells) > 1:
            po_details["PO Date"] = _clean_text(cells[1])
        elif cells and "PO Release Date :" in cells[0] and len(cells) > 1:
            po_details["PO Release Date"] = _clean_text(cells[1])
        elif cells and "Expected Delivery Date:" in cells[0] and len(cells) > 1:
            po_details["Expected Delivery Date"] = _clean_text(cells[1])
        elif cells and "PO Expiry Date:" in cells[0] and len(cells) > 1:
            po_details["PO Expiry Date"] = _normalize_excel_date(cells[1])

        if po_number_found and po_details.get("PO Expiry Date"):
            break

    billing_address_clean = ""
    shipping_address_clean = ""
    address_section = False
    for row in rows:
        cells = _row_cells(row)
        if not cells:
            continue
        if any(c.strip() == "Billing Address" for c in cells):
            address_section = True
            continue
        if address_section and len(cells) >= 2:
            billing_address = cells[0].split("\n")
            shipping_address = cells[1].split("\n")
            billing_address_clean = process_address(billing_address)
            shipping_address_clean = process_address(shipping_address)
            break
        if address_section and len(cells) == 1 and "SCOOTSY" in cells[0].upper():
            shipping_address_clean = process_address(cells[0].split("\n"))
            billing_address_clean = shipping_address_clean
            break

    main_address_match = re.search(
        r"SCOOTSY LOGISTICS PRIVATE LIMITED.*?(?: - |,\s*)\d{6}",
        shipping_address_clean,
        re.DOTALL | re.IGNORECASE,
    )
    main_address = main_address_match.group(0) if main_address_match else (shipping_address_clean or "Not Found")

    items: list[dict[str, Any]] = []
    item_section = total_section = False
    for row in rows:
        cells = _row_cells(row)
        if not cells:
            continue
        if any(c.strip() == "S." or c.startswith("S.") for c in cells):
            item_section = True
            continue
        if item_section and not total_section:
            if cells and "Total Amount (INR)" in cells[0]:
                total_section = True
                continue
            if len(cells) >= 15 and cells[0].isdigit():
                try:
                    items.append(
                        {
                            "Item Code": cells[1],
                            "Item Description": " ".join(cells[2].split()),
                            "HSN Code": cells[3],
                            "Quantity": int(float(cells[4])),
                            "MRP": float(cells[5]),
                            "Unit Base Cost (INR)": float(cells[6]),
                            "Taxable Value (INR)": float(cells[7]),
                            "CGST": float(cells[8]),
                            "SGST/UGST": float(cells[10]),
                            "IGST": float(cells[12]),
                            "CESS": float(cells[14]),
                            "Additional CESS": float(cells[16]),
                            "Total (INR)": float(cells[17]),
                        }
                    )
                except (ValueError, IndexError):
                    continue

    return {
        "PO Details": po_details,
        "Shipping Address": shipping_address_clean,
        "Billing Address": billing_address_clean,
        "Main Address": main_address,
        "Item Details": items,
    }


def read_xlsx_excel(xlsx_data: bytes) -> dict[str, Any]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_data), data_only=True)
    sheet = workbook.active
    rows = [list(row) for row in sheet.iter_rows(values_only=True)]

    po_details: dict[str, str] = {}
    label_keys = [
        ("PO No", "PO No"),
        ("PO Number", "PO No"),
        ("Vendor Name", "Vendor Name"),
        ("PO Date", "PO Date"),
        ("PO Release Date", "PO Release Date"),
        ("Expected Delivery Date", "Expected Delivery Date"),
        ("PO Expiry Date", "PO Expiry Date"),
    ]
    known_labels = {label.lower() for label, _ in label_keys}

    def looks_like_label(value: str) -> bool:
        text = _clean_text(value).rstrip(":").strip().lower()
        return any(text == label or text.startswith(label) for label in known_labels)

    for row in rows:
        for idx, cell in enumerate(row):
            if cell is None:
                continue
            cell_text = _clean_text(str(cell))
            for label, key in label_keys:
                if key in po_details:
                    continue
                inline_match = re.match(rf"^{re.escape(label)}\s*:\s*(.+)$", cell_text, re.I)
                if inline_match and not looks_like_label(inline_match.group(1)):
                    po_details[key] = _clean_text(inline_match.group(1))
                    continue
                if not re.search(rf"^{re.escape(label)}\s*:?\s*$", cell_text, re.I):
                    continue
                for next_cell in row[idx + 1 :]:
                    if next_cell is None or str(next_cell).strip() == "":
                        continue
                    next_text = _clean_text(str(next_cell))
                    if looks_like_label(next_text):
                        break
                    po_details[key] = next_text
                    break

    billing_address_clean = ""
    shipping_address_clean = ""
    for row_idx, row in enumerate(rows):
        cells = [_clean_text(str(c)) if c is not None else "" for c in row]
        if any(c == "Billing Address" for c in cells) and row_idx + 1 < len(rows):
            next_row = rows[row_idx + 1]
            non_empty = [c for c in next_row if c is not None and str(c).strip()]
            if len(non_empty) >= 1:
                billing_address_clean = str(non_empty[0]).strip()
            if len(non_empty) >= 2:
                shipping_address_clean = str(non_empty[1]).strip()
            elif len(non_empty) == 1:
                shipping_address_clean = billing_address_clean
            break

    if not shipping_address_clean and billing_address_clean:
        shipping_address_clean = billing_address_clean

    main_address_match = re.search(
        r"SCOOTSY LOGISTICS PRIVATE LIMITED.*?(?: - |,\s*)\d{6}",
        shipping_address_clean,
        re.DOTALL | re.IGNORECASE,
    )
    main_address = main_address_match.group(0) if main_address_match else (shipping_address_clean or "Not Found")

    header_row_idx = None
    header_map: dict[str, int] = {}
    for row_idx, row in enumerate(rows):
        cells = [_clean_text(str(c)) if c is not None else "" for c in row]
        if "Item Code" in cells and "Qty" in cells:
            header_row_idx = row_idx
            for col_idx, cell in enumerate(cells):
                if cell:
                    header_map[cell] = col_idx
            break

    items: list[dict[str, Any]] = []
    if header_row_idx is not None:

        def col(name: str, default: int | None = None) -> int | None:
            return header_map.get(name, default)

        start_idx = header_row_idx + 1
        if start_idx < len(rows):
            first_cells = [_clean_text(str(c)) if c is not None else "" for c in rows[start_idx]]
            if first_cells and first_cells[0] in {"No", "Rate"}:
                start_idx += 1

        for row in rows[start_idx:]:
            cells = list(row)
            serial = _clean_text(str(cells[0])) if cells and cells[0] is not None else ""
            if not serial.isdigit():
                row_text = " ".join(_clean_text(str(c)) for c in cells if c is not None)
                if "Total Amount" in row_text or row_text.startswith("Total"):
                    break
                continue

            def at(idx: int | None) -> Any:
                if idx is None or idx >= len(cells):
                    return None
                return cells[idx]

            items.append(
                {
                    "Item Code": _clean_text(str(at(col("Item Code", 1)) or "")),
                    "Item Description": _clean_text(str(at(col("Item Desc", 2)) or "")),
                    "HSN Code": _clean_text(str(at(col("HSN Code", 3)) or "")),
                    "Quantity": _to_int(at(col("Qty", 5))),
                    "MRP": _to_float(at(col("MRP", 6))),
                    "Unit Base Cost (INR)": _to_float(at(col("Unit Base Cost (INR)", 7))),
                    "Taxable Value (INR)": _to_float(at(col("Taxable Value (INR)", 9))),
                    "CGST": _to_float(at(col("CGST", 10))),
                    "SGST/UGST": _to_float(at(col("SGST/UGST", 13))),
                    "IGST": _to_float(at(col("IGST", 16))),
                    "CESS": _to_float(at(col("CESS", 19))),
                    "Additional CESS": _to_float(at(col("Additional CESS", 21))),
                    "Total (INR)": _to_float(at(col("Total (INR)", 22))),
                }
            )

    return {
        "PO Details": po_details,
        "Shipping Address": shipping_address_clean,
        "Billing Address": billing_address_clean,
        "Main Address": main_address,
        "Item Details": items,
    }


def apply_excel_to_header(header: dict[str, str], parsed: dict[str, Any]) -> None:
    """Merge Instamart Excel fields into PDF header (Excel preferred for ship-to)."""
    main = parsed.get("Main Address") or parsed.get("Shipping Address") or ""
    if main and main not in {"", "Not Found"}:
        header["delivered_to"] = main
    po_details = parsed.get("PO Details") or {}
    po_no = str(po_details.get("PO No") or "").strip()
    if po_no and str(header.get("po_number") or "") in {"", "Unknown", "Not Found"}:
        header["po_number"] = po_no
    expiry = _normalize_excel_date(str(po_details.get("PO Expiry Date") or ""))
    if expiry and expiry not in {"Invalid Date Format"}:
        if str(header.get("po_expiry_date") or "") in {"", "Not Found", "Invalid Date Format"}:
            header["po_expiry_date"] = expiry
    shipping = str(parsed.get("Shipping Address") or "")
    if shipping and str(header.get("gst_no") or "") in {"", "Not Found"}:
        gst = extract_gst_from_text(shipping)
        if gst and gst != "Not Found":
            header["gst_no"] = gst


def parse_excel_attachment(excel_bytes: bytes, filename: str) -> dict[str, Any]:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or excel_bytes[:2] == b"PK":
        return read_xlsx_excel(excel_bytes)
    return read_xml_excel(excel_bytes.decode("utf-8", errors="replace"))


def lines_from_item_details(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "item_code": str(item.get("Item Code") or ""),
                "quantity": _to_int(item.get("Quantity")),
                "discount": 0,
                "product_description": str(item.get("Item Description") or ""),
                "basic_cost_price": _to_float(item.get("Unit Base Cost (INR)")),
                "igst_percentage": _to_float(item.get("IGST")),
                "tax_amount": _to_float(item.get("Taxable Value (INR)")),
                "landing_rate": 0,
                "mrp": _to_float(item.get("MRP")),
                "total_amount": _to_float(item.get("Total (INR)")),
            }
        )
    return rows

"""Flipkart PO intake, all in this one file.

Flow for every email in the Flipkart mailbox label:
    1. connect to the mailbox            (connect_mailbox)
    2. take the Excel attachment         (find_excel_attachment)
    3. read it with xlrd / openpyxl      (load_rows, parse_excel)
    4. run the checks                    (run_checks): address, item mapping, tax code
    5. save the Excel to S3, PO to RDS   (save_po)

Credentials live in SSM ``/b2b/imap``::

    {"email": ..., "password": ..., "imap_server": ..., "imap_port": 993,
     "folders": {"flipkart": "<gmail label>"}, "card_codes": {"flipkart": "<SAP CardCode>"}}

Layout of the Flipkart sheet (top to bottom):
    PO#<number> ... PO Expiry: <date>
    SHIPPED TO ADDRESS | <address text> GSTIN ... STATE CODE ...
    <header row containing "HSN/SA Code"> then one row per item
    "Total Quantity=..." marks the end of the items
"""

from __future__ import annotations

import email
import imaplib
import io
import json
import logging
import os
import re
import textwrap
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import intake_validate
import store
from gst_validate import extract_gst_from_text, validate_gst_match
from hana_lookup import SapError, get_business_partner, get_items

logger = logging.getLogger(__name__)

CHANNEL = "flipkart"
IMAP_PARAM_NAME = os.environ.get("B2B_IMAP_PARAM_NAME", "/b2b/imap")
ADDRESS_MATCH_MIN_SCORE = 0.6  # ship-to text must be at least this similar to a saved address

# The address cell also holds other blocks; the address ends where one of these starts.
_ADDRESS_STOP_WORDS = ("GSTIN", "STATE CODE", "ORDER DETAILS", "BILLED TO ADDRESS")
# Rows that mean "no more items".
_ITEMS_END_WORDS = ("total quantity=", "important notification")
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"  # old binary Excel (.xls) files start with these bytes


# --- 1. mailbox ---------------------------------------------------------------------------


def load_mail_settings() -> dict[str, Any]:
    """Read the mailbox settings JSON from SSM."""
    import boto3

    raw = boto3.client("ssm").get_parameter(Name=IMAP_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    settings = json.loads(raw)
    if not isinstance(settings, dict):
        raise RuntimeError(f"SSM {IMAP_PARAM_NAME} must be a JSON object")
    return settings


def card_code(settings: dict[str, Any]) -> str:
    code = str((settings.get("card_codes") or {}).get(CHANNEL) or "").strip()
    if not code:
        raise RuntimeError(f"SSM {IMAP_PARAM_NAME} must set card_codes.{CHANNEL} (SAP CardCode for Flipkart)")
    return code


def mailbox_label(settings: dict[str, Any]) -> str:
    folder = str((settings.get("folders") or {}).get(CHANNEL) or "").strip()
    if not folder:
        raise RuntimeError(f"SSM {IMAP_PARAM_NAME} must set folders.{CHANNEL} (Gmail label for Flipkart)")
    return folder


def connect_mailbox(settings: dict[str, Any]) -> imaplib.IMAP4_SSL:
    """Log in and open the Flipkart label."""
    folder = mailbox_label(settings)
    mail = imaplib.IMAP4_SSL(settings["imap_server"], int(settings.get("imap_port") or 993))
    mail.login(settings["email"], settings["password"])
    status, detail = mail.select(f'"{folder}"')
    if status != "OK":
        # imaplib does not raise when a label cannot be opened, so check it here.
        _status, labels = mail.list()
        names = [label.decode().rsplit(' "/" ', 1)[-1] for label in labels or []]
        raise RuntimeError(f"Could not open mailbox label {folder!r} ({detail}). Labels in this mailbox: {names}")
    return mail


def find_excel_attachment(msg: email.message.Message) -> bytes | None:
    """Return the bytes of the first .xls / .xlsx attachment, or None."""
    for part in msg.walk():
        name = part.get_filename() or ""
        payload = part.get_payload(decode=True)
        if name.lower().endswith((".xls", ".xlsx")) and isinstance(payload, bytes):
            return payload
    return None


# --- 2. reading the file into rows of text (xlrd for .xls, openpyxl for .xlsx) -------------


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def _to_float(value: Any) -> float:
    """Number from a cell. Flipkart writes '949.0 INR' and '2.5%', so keep only digits, '.' and '-'."""
    try:
        return round(float(re.sub(r"[^0-9.\-]", "", str(value))), 2)
    except ValueError:
        return 0.0


def _to_int(value: Any) -> int:
    return int(_to_float(value))


def _rows_from_xlsx(excel_bytes: bytes) -> list[list[str]]:
    """.xlsx is a zip file: read the first sheet with openpyxl."""
    import openpyxl

    book = openpyxl.load_workbook(io.BytesIO(excel_bytes), data_only=True)
    rows = [[_clean(cell) for cell in row] for row in book.worksheets[0].iter_rows(values_only=True)]
    return [row for row in rows if any(row)]


def _rows_from_xls(excel_bytes: bytes) -> list[list[str]]:
    """.xls is the old binary format: read the first sheet with xlrd."""
    import xlrd

    sheet = xlrd.open_workbook(file_contents=excel_bytes).sheet_by_index(0)
    rows = [
        [_clean(sheet.cell_value(r, c)) for c in range(sheet.ncols)]
        for r in range(sheet.nrows)
    ]
    return [row for row in rows if any(row)]


def load_rows(excel_bytes: bytes) -> list[list[str]]:
    """Return the sheet as rows of cleaned strings; the reader is chosen from the file content."""
    if excel_bytes[:4] == _XLS_MAGIC:
        return _rows_from_xls(excel_bytes)
    return _rows_from_xlsx(excel_bytes)


# --- 3. picking the fields out of the rows -------------------------------------------------


def _parse_expiry(raw: str) -> str:
    """Turn the PO expiry text into YYYY-MM-DD. Flipkart writes day-month-year: '19-10-26' = 19 Oct 2026."""
    for fmt in ("%d-%m-%y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "Invalid Date Format"


def _find_po_number(rows: list[list[str]]) -> str:
    for row in rows:
        match = re.search(r"PO#\s*([A-Z0-9]+)", " ".join(row))
        if match:
            return match.group(1)
    return "Not Found"


def _find_po_expiry(rows: list[list[str]]) -> str:
    for row in rows:
        match = re.search(r"PO Expiry\s*[:\-]?\s*([\d\-]+)", " ".join(row))
        if match:
            return _parse_expiry(match.group(1))
    return "Not Found"


def _find_shipped_to(rows: list[list[str]]) -> tuple[str, str, str]:
    """Return (address, gst_no, state_code) from the 'SHIPPED TO ADDRESS' block.

    The label can be in any column (it shares a row with 'BILLED TO ADDRESS'). Everything to its
    right on that row is: the address, then 'GSTIN' + number, then 'State Code' + code.
    """
    for row in rows:
        label = next((i for i, cell in enumerate(row) if "SHIPPED TO ADDRESS" in cell.upper()), None)
        if label is None:
            continue
        right_side = " ".join(cell for cell in row[label + 1 :] if cell)
        address = right_side
        for word in _ADDRESS_STOP_WORDS:
            position = address.upper().find(word)
            if position != -1:
                address = address[:position]
        # extract_gst_from_text looks for a "Shipping Address" label before the GSTIN.
        gst_no = extract_gst_from_text(f"Shipping Address: {right_side}")
        state = re.search(r"State Code\s*:?\s*(?:IN-)?([A-Z]{2})\b", right_side, re.I)  # 'State Code IN-MH'
        return address.strip(), gst_no, state.group(1).upper() if state else ""
    return "", "Not Found", ""


def _column(header: list[str], *names: str) -> int | None:
    """Index of the first header cell whose name matches (ignoring case, spaces, punctuation)."""
    wanted = {re.sub(r"[^a-z0-9]", "", name.lower()) for name in names}
    for index, cell in enumerate(header):
        if re.sub(r"[^a-z0-9]", "", cell.lower()) in wanted:
            return index
    return None


def _find_lines(rows: list[list[str]]) -> list[dict[str, Any]]:
    header_index = next((i for i, row in enumerate(rows) if "HSN/SA Code" in " ".join(row)), None)
    if header_index is None:
        return []
    header = rows[header_index]

    fsn_col = _column(header, "FSN/ISBN13", "FSN")
    qty_col = _column(header, "Quantity")
    title_col = _column(header, "Title", "Description", "Product Title")
    mrp_col = _column(header, "Supplier MRP")
    price_col = _column(header, "Supplier Price")
    taxable_col = _column(header, "Taxable Value")
    igst_col = _column(header, "IGST Rate")
    sgst_col = _column(header, "SGST/UTGST Rate")
    cgst_col = _column(header, "CGST Rate")
    tax_col = _column(header, "Tax Amount")
    total_col = _column(header, "Total Amount")

    def cell(row: list[str], col: int | None) -> str:
        return row[col] if col is not None and col < len(row) else ""

    lines: list[dict[str, Any]] = []
    for row in rows[header_index + 1 :]:
        row_text = " ".join(row).lower()
        if any(word in row_text for word in _ITEMS_END_WORDS):
            break
        fsn = cell(row, fsn_col)
        if not fsn:
            continue
        quantity = _to_int(cell(row, qty_col))
        total_amount = _to_float(cell(row, total_col))
        # 'Supplier Price' already includes GST. SAP adds GST itself, so SAP needs the price before tax.
        price_before_tax = round(_to_float(cell(row, taxable_col)) / quantity, 4) if quantity else 0
        gst_rate = _to_float(cell(row, igst_col)) or _to_float(cell(row, sgst_col)) + _to_float(cell(row, cgst_col))
        lines.append(
            {
                "columns": {name: cell(row, i) for i, name in enumerate(header) if name},  # the whole row, as in the file
                "item_code": fsn,
                "quantity": quantity,
                "discount": 0,
                "product_description": cell(row, title_col),
                "basic_cost_price": price_before_tax,
                "igst_percentage": gst_rate,
                "tax_amount": _to_float(cell(row, tax_col)),
                "landing_rate": _to_float(cell(row, price_col)),
                "mrp": _to_float(cell(row, mrp_col)),
                "total_amount": total_amount,
            }
        )
    return lines


# Labels of the header block of the Flipkart sheet. A value is whatever follows its label up to the next label.
_HEADER_LABELS = {
    "registered address", "po#", "nature of supply", "nature of transaction", "po expiry", "category", "order date",
    "supplier name", "supplier address", "supplier contact", "email", "billed by", "gstin", "state code",
    "shipped from", "billed to address", "shipped to address", "mode of payment", "contract ref id",
    "contract version", "credit term", "name", "contact",
}
_ADDRESS_BLOCKS = {"billed by", "shipped from", "billed to address", "shipped to address"}  # each has its own GSTIN/State Code
_REPEATED_LABELS = {"name", "email", "contact"}  # also used under PAYMENT / APPROVAL DETAILS


def _find_all_header_fields(rows: list[list[str]]) -> dict[str, str]:
    """Every 'label -> value' pair above the items table (supplier, addresses, payment, approval...)."""
    end = next((i for i, row in enumerate(rows) if "HSN/SA Code" in " ".join(row)), len(rows))
    fields: dict[str, str] = {}

    def put(key: str, value: str) -> None:
        while key in fields:
            key += " (2)"
        fields[key] = value

    section = ""
    for row in rows[:end]:
        cells = [cell for cell in row if cell]
        if len(cells) == 1 and cells[0].lower() not in _HEADER_LABELS and cells[0].isupper():
            section = cells[0].title()  # e.g. 'Payment Details', 'Approval Details'
            continue
        block, label, values, leading = "", "", [], []

        def flush() -> None:
            if not label:
                return
            key = label
            if label.lower() in ("gstin", "state code") and block:
                key = f"{block} - {label}"
            elif label.lower() in _REPEATED_LABELS and section:
                key = f"{section} - {label}"
            put(key, " ".join(values).strip())

        for cell in cells:
            if cell.lower() in _HEADER_LABELS:
                flush()
                if leading:  # text before the first label, e.g. 'Flipkart  PURCHASE ORDER # ...'
                    put("Title", " ".join(leading))
                    leading = []
                label, values = cell, []
                if cell.lower() in _ADDRESS_BLOCKS:
                    block = cell
            elif label:
                values.append(cell)
            else:
                leading.append(cell)
        if leading:
            put("Title", " ".join(leading))
        flush()
    return fields


def _find_totals(rows: list[list[str]]) -> dict[str, str]:
    """The 'Total Quantity= / Total=' row under the items."""
    out: dict[str, str] = {}
    for row in rows:
        cells = [cell for cell in row if cell]
        text = " ".join(cells).lower()
        if "total quantity=" in text:
            out["Total Quantity"] = cells[1] if len(cells) > 1 else ""
            amounts = cells[cells.index(next(c for c in cells if c.lower().startswith("total="))) + 1 :] if "total=" in text else []
            for name, value in zip(("Total Taxable Value", "Total Tax", "Total Amount"), amounts):
                out[name] = value
    return out


def parse_excel(excel_bytes: bytes) -> dict[str, Any]:
    """Read a Flipkart PO Excel. Returns the header fields plus ``lines``."""
    try:
        rows = load_rows(excel_bytes)
    except Exception as exc:
        raise ValueError(f"Could not read Flipkart Excel (.xls/.xlsx): {exc}") from exc

    po_number = _find_po_number(rows)
    if po_number == "Not Found":
        raise ValueError("Not a Flipkart PO Excel (missing PO#)")

    delivered_to, gst_no, state_code = _find_shipped_to(rows)
    return {
        "company_name": "Flipkart",
        "contact_name": "Not Found",
        "po_number": po_number,
        "po_expiry_date": _find_po_expiry(rows),
        "delivered_to": delivered_to,
        "gst_no": gst_no,
        "state_code": state_code,
        "all_header_fields": _find_all_header_fields(rows),
        "totals": _find_totals(rows),
        "lines": _find_lines(rows),
    }


# --- 4. checks before the PO is saved -----------------------------------------------------


def match_address(delivered_to: str, aliases: list[dict[str, str]]) -> str:
    """Return the address code of the most similar saved ship-to address, or '' if none is close.

    Flipkart warehouses in one city have almost identical text, so a saved address whose pincode
    differs from the PO's pincode is never a match.
    """
    pincode = re.search(r"\b\d{6}\b", delivered_to)
    best_code, best_score = "", 0.0
    for alias in aliases:
        saved = alias.get("address_entry") or ""
        saved_pincodes = re.findall(r"\b\d{6}\b", saved)
        if pincode and saved_pincodes and pincode.group() not in saved_pincodes:
            continue
        score = SequenceMatcher(None, delivered_to.lower(), saved.lower()).ratio()
        if score > best_score:
            best_code, best_score = alias.get("address_id") or "", score
    return best_code if best_score > ADDRESS_MATCH_MIN_SCORE else ""


def tax_code(state: str, tax_rate: int) -> str:
    """Karnataka (KT) is intrastate: KACS<rate>. Every other state is interstate: KAIG<rate>."""
    return f"{'KACS' if state.upper() == 'KT' else 'KAIG'}{tax_rate}"


def check_address(parsed: dict[str, Any], aliases: list[dict[str, str]], code: str) -> tuple[str, str, str]:
    """Match the PO ship-to to a saved address, then confirm SAP has that address code.

    Returns (sap address name, state, error). The state decides intrastate / interstate tax.
    """
    address_id = match_address(parsed["delivered_to"], aliases)
    if not address_id:
        return "", "", "No sufficiently similar address entry found."
    try:
        partner = get_business_partner(code)
    except SapError as exc:
        return "", "", str(exc)
    sap_address = next(
        (a for a in partner.get("BPAddresses") or [] if str(a.get("AddressName") or "").strip() == address_id),
        None,
    )
    if not sap_address:
        return "", "", f"No matching SAP address found for '{address_id}'."
    gst_error = validate_gst_match(parsed["gst_no"], sap_address, partner)
    return sap_address.get("AddressName") or "", sap_address.get("State") or "", gst_error or ""


# --- 4b. a PO address that SAP does not have yet: add it to the customer in SAP -------------


GSTIN_FORMAT = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")


def state_for_gstin(gstin: str, sap_addresses: list[dict[str, Any]]) -> str:
    """SAP state code (KT, DL, ...) of an address that already uses this GSTIN, or ''."""
    for address in sap_addresses:
        if (address.get("GSTIN") or "").strip().upper() == gstin.strip().upper() and address.get("State"):
            return address["State"]
    return ""


def build_new_address(
    parsed: dict[str, Any],
    address_name: str,
    sap_addresses: list[dict[str, Any]],
) -> dict[str, Any]:
    """The SAP ship-to address row for this PO. Raises ValueError when something SAP needs is missing.

    The GSTIN is either already on one of the customer's SAP addresses (its state is copied), or it is
    new: then it must belong to the same company (same PAN) and its state is the 'State Code' of the PO.
    """
    name = (address_name or "").strip()
    if not name:
        raise ValueError("Address name is empty. Use SAP's style, e.g. 'Flipkart BLR Anekal (FBAWG)'.")
    if len(name) > 50:
        raise ValueError("SAP address names can be at most 50 characters.")
    if any(str(a.get("AddressName") or "").strip().lower() == name.lower() for a in sap_addresses):
        raise ValueError(f"SAP already has an address named {name!r}.")

    gstin = parsed["gst_no"].strip().upper()
    if not GSTIN_FORMAT.match(gstin):
        raise ValueError(f"GSTIN {parsed['gst_no']!r} on the PO is missing or not a valid GSTIN.")
    state = state_for_gstin(gstin, sap_addresses)
    if not state:  # a GSTIN SAP has not seen yet: only accept it for the same company (same PAN)
        known_pans = {(a.get("GSTIN") or "")[2:12] for a in sap_addresses if a.get("GSTIN")}
        if gstin[2:12] not in known_pans:
            raise ValueError(f"GSTIN {gstin} belongs to a different company (PAN {gstin[2:12]}) than this customer.")
        state = parsed.get("state_code", "")
        if not state:
            raise ValueError(f"GSTIN {gstin} is new to SAP and the PO has no State Code to use for it.")

    # The PO address ends '... <pincode> <city> <pincode>': street is up to the first pincode.
    text = parsed["delivered_to"]
    pin = re.search(r"\b\d{6}\b", text)
    if not pin:
        raise ValueError("No 6-digit pincode found in the PO address.")
    street_lines = textwrap.wrap(text[: pin.end()], 100)
    return {
        "AddressName": name,
        "AddressType": "bo_ShipTo",
        "Street": street_lines[0],
        "Block": " ".join(street_lines[1:])[:100],
        "City": re.sub(r"\b\d{6}\b", "", text[pin.end():]).strip(),
        "ZipCode": pin.group(),
        "State": state,
        "Country": "IN",
        "GSTIN": gstin,
    }


def add_address_to_sap(code: str, parsed: dict[str, Any], address_name: str) -> dict[str, Any]:
    """Add the PO's ship-to address to the SAP customer, then read it back to confirm."""
    partner = get_business_partner(code)
    before = partner.get("BPAddresses") or []
    new_address = build_new_address(parsed, address_name, before)
    import b2b_sappost  # shares the Service Layer login with order posting

    status, body = b2b_sappost._request("PATCH", f"BusinessPartners('{code}')", body={"BPAddresses": [new_address]})
    if status >= 300:
        raise RuntimeError(f"SAP refused the new address: {b2b_sappost._error_text(body)}")

    # Read it back: the new address must exist and none of the old ones may be gone.
    after = get_business_partner(code).get("BPAddresses") or []
    names_before = {str(a.get("AddressName")).strip() for a in before}
    names_after = {str(a.get("AddressName")).strip() for a in after}
    if address_name.strip() not in names_after or not names_before <= names_after:
        raise RuntimeError(
            f"SAP accepted the request but the addresses look wrong afterwards "
            f"(new present: {address_name.strip() in names_after}, lost: {sorted(names_before - names_after)})."
        )
    return {"address": new_address, "addresses_before": len(before), "addresses_after": len(after)}


def suggest_address_name(parsed: dict[str, Any]) -> str:
    """SAP-style name 'Flipkart <City> (<warehouse code>)'.

    The warehouse code is the start of the PO number without its trailing N (FNNWN0852... -> FNNW).
    The PO has no area name like 'Anekal', so the city is used instead.
    """
    letters = re.match(r"[A-Z]+", parsed["po_number"])
    code = letters.group() if letters else ""
    code = code[:-1] if code.endswith("N") and len(code) > 2 else code
    pin = re.search(r"\b\d{6}\b", parsed["delivered_to"])
    city = re.sub(r"\b\d{6}\b", "", parsed["delivered_to"][pin.end():]).strip().title() if pin else ""
    return " ".join(part for part in ("Flipkart", city, f"({code})" if code else "") if part)[:50]


def ensure_address_in_sap(conn, parsed: dict[str, Any], code: str) -> str:
    """Make sure the PO's ship-to exists in SAP and in the ship-to map; add what is missing.

    Returns an error text ('' = fine). Needs no approval: an address is only created when its GSTIN
    already belongs to this customer in SAP, so an email cannot add an unrelated address.
    """
    if os.environ.get("FLIPKART_AUTO_ADD_ADDRESS", "1") == "0":
        return ""
    aliases = store.list_ship_to_aliases(conn, CHANNEL)
    try:
        sap_names = {str(a.get("AddressName") or "").strip() for a in get_business_partner(code).get("BPAddresses") or []}
        name = match_address(parsed["delivered_to"], aliases) or suggest_address_name(parsed)
        if name not in sap_names:
            result = add_address_to_sap(code, parsed, name)
            logger.info("Added SAP address %r to %s: %s", name, code, result["address"])
        if not any(alias["address_id"] == name for alias in aliases):
            store.upsert_ship_to_alias(conn, name, parsed["delivered_to"], channel=CHANNEL)
    except (SapError, ValueError, RuntimeError) as exc:
        return f"Could not add the ship-to address to SAP: {exc}"
    return ""


def check_items(lines: list[dict[str, Any]], sku_map: dict[str, str], state: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Map each Flipkart item code to a SAP code and add the SAP name and tax code.

    Returns (lines, error messages).
    """
    errors: list[str] = []
    unmapped = sorted({row["item_code"] for row in lines if not sku_map.get(row["item_code"])})
    if unmapped:
        errors.append(f"No mapping found for item codes: {', '.join(unmapped)}")

    sap_codes = [sku_map[row["item_code"]] for row in lines if sku_map.get(row["item_code"])]
    try:
        sap_items = get_items(sap_codes) if sap_codes else {}
    except SapError as exc:
        sap_items = {}
        errors.append(str(exc))

    no_tax: list[str] = []
    checked: list[dict[str, Any]] = []
    for row in lines:
        sap_code = sku_map.get(row["item_code"], "")
        item = sap_items.get(sap_code) or {}
        rate_text = str(item.get("U_TaxRate") if item.get("U_TaxRate") is not None else "").strip()
        has_rate = bool(sap_code) and rate_text.replace(".", "", 1).isdigit()
        if sap_code and not has_rate:
            no_tax.append(sap_code)
        checked.append(
            {
                **row,
                "sap_code": sap_code,
                "item_name": item.get("ItemName") or "",
                "tax_rate": int(float(rate_text)) if has_rate else "",
                "gst_tax": item.get("U_GstTax") or "",
                "igst_tax": item.get("U_IgstTax") or "",
                "taxcode": tax_code(state, int(float(rate_text))) if has_rate and state else "",
            }
        )
    if no_tax:
        errors.append(f"Missing tax code from SAP item master (OITM) for: {', '.join(sorted(set(no_tax)))}")
    return checked, errors


def run_checks(conn, parsed: dict[str, Any], code: str) -> dict[str, Any]:
    """Run all checks. Returns the address result, the checked lines and one error text ('' = all good)."""
    errors = intake_validate.validate_intake_data(CHANNEL, parsed, parsed["lines"])
    add_error = ensure_address_in_sap(conn, parsed, code)
    address_name, state, address_error = check_address(parsed, store.list_ship_to_aliases(conn, CHANNEL), code)
    if add_error or address_error:
        errors.append(add_error or address_error)
    lines, item_errors = check_items(parsed["lines"], store.sku_map_dict(conn, CHANNEL), state)
    return {
        "matched_address_name": address_name,
        "matched_state": state,
        "lines": lines,
        "last_error": " ".join(errors + item_errors),
    }


# --- 5. saving: S3 + RDS --------------------------------------------------------------------


def _s3_key(po_number: str, excel_bytes: bytes) -> str:
    extension = "xls" if excel_bytes[:4] == _XLS_MAGIC else "xlsx"
    return f"{CHANNEL}/Flipkart_{po_number}.{extension}"


def _save_to_s3(key: str, body: bytes) -> None:
    import boto3

    boto3.client("s3").put_object(Bucket=os.environ["B2B_DOCS_BUCKET"], Key=key, Body=body)


def _read_from_s3(key: str) -> bytes:
    import boto3

    return boto3.client("s3").get_object(Bucket=os.environ["B2B_DOCS_BUCKET"], Key=key)["Body"].read()


def _pipeline_status(last_error: str, lines: list) -> str:
    if last_error:
        return store.STATUS_NEEDS_REVIEW
    return store.STATUS_READY if lines else store.STATUS_PARSED


def save_po(
    conn, parsed: dict[str, Any], checks: dict[str, Any], *, code: str, s3_key: str, uid: str, subject: str, amend_status: str = ""
) -> dict[str, Any]:
    """Write the PO header and its lines to RDS (existing b2b.purchase_orders / b2b.po_lines)."""
    lines = checks["lines"]
    expiry = parsed["po_expiry_date"]
    state = checks["matched_state"]
    header = {
        "channel": CHANNEL,
        "po_number": parsed["po_number"],
        "pipeline_status": _pipeline_status(checks["last_error"], lines),
        "company_name": parsed["company_name"],
        "contact_name": parsed["contact_name"],
        "po_expiry_date": None if expiry in {"Not Found", "Invalid Date Format"} else expiry,
        "delivered_to": parsed["delivered_to"],
        "gst_no": parsed["gst_no"],
        "customer_code": code,
        "matched_address_name": checks["matched_address_name"],
        "matched_state": state,
        "billing_from": "KT",
        "bill_to": "Local" if state.upper() == "KT" else "Central",
        "email_uid": uid,
        "email_subject": subject,
        "xlsx_s3_key": s3_key,
        "total_quantity": sum(row["quantity"] for row in lines),
        "total_amount": sum(row["total_amount"] for row in lines),
        "total_items": len({row["item_code"] for row in lines}),
        "last_error": checks["last_error"],
    }
    if amend_status:  # an amended PO replaces the old one, even when it was already posted
        header["duplicate_records_status"] = amend_status
    return store.upsert_purchase_order(conn, header, lines, force_update=bool(amend_status))


# --- team email, duplicate / amended POs, moving finished mail --------------------------------

_EMAIL_TEMPLATE = Path(__file__).with_name("email_template.html")
AMEND_WORDS = ("amend", "revis")  # amend, amended, amendment, revise, revised


def is_amend_subject(subject: str) -> bool:
    """A second email for a PO only replaces it when the subject says it is an amendment."""
    return any(word in (subject or "").lower() for word in AMEND_WORDS)


def _alert_recipients() -> list[str]:
    return [addr.strip() for addr in os.environ.get("B2B_ALERT_ADMIN_EMAIL", "").split(",") if addr.strip()]


def send_po_email(po_number: str, message: str, file_url: str = "") -> bool:
    """Email the team through SES (same sender, recipients and template as the other channels)."""
    sender, recipients = os.environ.get("B2B_ALERT_FROM_EMAIL", "").strip(), _alert_recipients()
    if not (sender and recipients):
        return False  # not configured: the dashboard Inbox is the only notification
    html = _EMAIL_TEMPLATE.read_text(encoding="utf-8") if _EMAIL_TEMPLATE.exists() else ""
    link = f'<a href="{file_url}" target="_blank" rel="noopener noreferrer">Open PO file</a>' if file_url else "N/A"
    html = (
        html.replace("{{ recipient_name }}", "Team")
        .replace('{{ po_number or "N/A" }}', po_number or "N/A")
        .replace('{{ error_message or "No details provided." }}', message or "No details provided.")
        .replace("{{ po_link_html }}", link)
        .replace("{{ channel_name }}", "Flipkart")
    )
    try:
        import boto3

        boto3.client("ses", region_name=os.environ.get("B2B_SES_REGION", "ap-south-1")).send_email(
            Source=sender,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": f"Notification from B2B Flipkart Channel - PO Number: {po_number}"},
                "Body": {"Text": {"Data": f"PO {po_number}\n\n{message}\n\n{file_url}"}, "Html": {"Data": html or message}},
            },
        )
        return True
    except Exception:
        logger.error("SES email failed for PO %s", po_number, exc_info=True)
        return False


def _presigned_url(s3_key: str) -> str:
    """Link to the stored Excel for the email, valid for an hour."""
    if not s3_key:
        return ""
    try:
        import boto3

        return boto3.client("s3").generate_presigned_url(
            "get_object", Params={"Bucket": os.environ["B2B_DOCS_BUCKET"], "Key": s3_key}, ExpiresIn=3600
        )
    except Exception:
        logger.warning("Could not presign %s", s3_key, exc_info=True)
        return ""


def email_team(conn, po_number: str, message: str, s3_key: str = "") -> None:
    """Send the team email and record it in b2b.email_log. Does nothing when email is not configured."""
    recipients = _alert_recipients()
    if not (recipients and os.environ.get("B2B_ALERT_FROM_EMAIL", "").strip()):
        return
    sent = send_po_email(po_number, message, _presigned_url(s3_key))
    store.add_email_log(
        conn, recipients=",".join(recipients), subject=f"Flipkart PO {po_number}", po_number=po_number, sent_ok=sent, channel=CHANNEL
    )


def notify_inbox(
    conn, po_number: str, status: str, last_error: str, previous: dict[str, Any] | None, *, s3_key: str = "", amend_status: str = ""
) -> None:
    """Dashboard Inbox row (one per PO) plus the team email. Skipped when a re-fetch changed nothing."""
    if amend_status:
        kind, title = "warning", f"Amended PO {po_number}"
        body = f"Email subject contains 'Amend' or 'Revised'. Proceeding to update records. Status: {amend_status}."
        message = body + (f" {last_error}" if last_error else "")
    elif previous and previous.get("pipeline_status") == status and (previous.get("last_error") or "") == last_error:
        return
    else:
        kind = "error" if last_error else "success"
        title = ("Needs attention: PO " if last_error else "Stored: PO ") + po_number
        body = message = last_error or f"Stored with status {status}"
    store.add_notification(conn, kind=kind, title=title, body=body, po_number=po_number, channel=CHANNEL)
    email_team(conn, po_number, message, s3_key)


def _warning_exists(conn, po_number: str, title: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM b2b.notifications WHERE channel = %s AND po_number = %s AND title = %s", (CHANNEL, po_number, title))
        return cur.fetchone() is not None


def skip_duplicate(conn, uid: str, po_number: str, existing_header: dict[str, Any]) -> dict[str, Any]:
    """A PO that already exists arrived again without 'Amend'/'Revised' in the subject: leave it alone, say so.

    The email is not marked processed, so it stays in the label until someone retitles or removes it.
    The warning (and its email) is sent once, not on every fetch.
    """
    title = f"Skipped duplicate PO {po_number}"
    if not _warning_exists(conn, po_number, title):
        message = (
            "Email subject does not contain 'Amend' or 'Revised'. "
            "Skipping duplicate PO update. Add Amend/Revised to the subject to replace this PO."
        )
        store.add_notification(conn, kind="warning", title=title, body=message, po_number=po_number, channel=CHANNEL)
        email_team(conn, po_number, message, existing_header.get("xlsx_s3_key") or "")
        conn.commit()
    return {"uid": uid, "po_number": po_number, "status": "skipped", "reason": "duplicate_without_amend_subject", "mail_moved": False}


def move_enabled() -> bool:
    return os.environ.get("B2B_MOVE_PROCESSED_MAIL", "").strip().lower() in {"1", "true", "yes"}


def mail_is_ready(status: str, last_error: str) -> bool:
    """Only a PO that is ready to post, with no error, has its email moved out of the label."""
    return status == store.STATUS_READY and not (last_error or "").strip()


def move_to_processed(mail: imaplib.IMAP4_SSL, uid: str, folder: str) -> bool:
    """Move one email from the label to '<label>-Processed'. A failure here never fails the PO."""
    destination = f"{folder}-Processed"
    try:
        try:
            mail.create(f'"{destination}"')
        except imaplib.IMAP4.error:
            pass  # already exists
        if mail.uid("copy", uid, f'"{destination}"')[0] != "OK":
            return False
        if mail.uid("store", uid, "+FLAGS", r"(\Deleted)")[0] != "OK":
            return False
        mail.expunge()
        return True
    except Exception:
        logger.warning("Could not move email %s to %s", uid, destination, exc_info=True)
        return False


def move_ready_mails(conn, mail: imaplib.IMAP4_SSL, uids: list[str], folder: str) -> int:
    """After a fetch, also move emails whose PO became ready later (maps fixed, then Recheck)."""
    if not move_enabled():
        return 0
    moved = 0
    with conn.cursor() as cur:
        for uid in uids:
            cur.execute(
                "SELECT pipeline_status, COALESCE(last_error, '') FROM b2b.purchase_orders WHERE channel = %s AND email_uid = %s LIMIT 1",
                (CHANNEL, uid),
            )
            row = cur.fetchone()
            if row and mail_is_ready(row[0], row[1]) and move_to_processed(mail, uid, folder):
                moved += 1
    return moved


def save_failure(conn, uid: str, subject: str, message: str, code: str) -> dict[str, Any]:
    """Keep an unreadable email visible on the dashboard so someone can look at it."""
    po_number = f"UNPARSED-{CHANNEL}-{uid}"
    previous = (store.get_po(conn, po_number, channel=CHANNEL) or {}).get("header")
    store.upsert_purchase_order(
        conn,
        {
            "channel": CHANNEL,
            "po_number": po_number,
            "pipeline_status": store.STATUS_FAILED,
            "customer_code": code,
            "email_uid": uid,
            "email_subject": subject,
            "last_error": message,
        },
        [],
    )
    if (previous or {}).get("last_error") != message:
        store.add_notification(
            conn, kind="error", title="Could not extract purchase order", body=message, po_number=po_number, channel=CHANNEL
        )
    conn.commit()
    return {"uid": uid, "po_number": po_number, "status": store.STATUS_FAILED, "error": message}


# --- fetch: the entry point ------------------------------------------------------------------


def _decode_subject(msg: email.message.Message) -> str:
    from email.header import decode_header, make_header

    return str(make_header(decode_header(msg.get("Subject") or "")))


def process_email(conn, mail: imaplib.IMAP4_SSL, uid: str, code: str, folder: str = "") -> dict[str, Any]:
    _status, data = mail.uid("fetch", uid, "(RFC822)")
    raw = next((part[1] for part in data or [] if isinstance(part, tuple)), None)
    if not raw:
        return {"uid": uid, "status": "empty"}
    msg = email.message_from_bytes(raw)
    subject = _decode_subject(msg)

    excel_bytes = find_excel_attachment(msg)
    if not excel_bytes:
        return save_failure(conn, uid, subject, "This email has no Excel (.xls or .xlsx) attachment.", code)
    try:
        parsed = parse_excel(excel_bytes)
    except ValueError as exc:
        return save_failure(conn, uid, subject, str(exc), code)

    po_number = parsed["po_number"]
    existing = store.get_po(conn, po_number, channel=CHANNEL)
    previous, amend_status = (existing or {}).get("header"), ""
    if existing:
        if not is_amend_subject(subject):
            return skip_duplicate(conn, uid, po_number, existing["header"])
        # An amended PO: keep a snapshot of the old one, then replace it (even if it was posted).
        amend_status = store.archive_po_for_amend(conn, existing, email_subject=subject, email_uid=uid)
        store.add_audit(conn, actor="fetch", action="amend_archive", po_number=po_number, detail=amend_status, channel=CHANNEL)
        previous = None

    s3_key = _s3_key(po_number, excel_bytes)
    _save_to_s3(s3_key, excel_bytes)
    checks = run_checks(conn, parsed, code)
    record = save_po(conn, parsed, checks, code=code, s3_key=s3_key, uid=uid, subject=subject, amend_status=amend_status)
    store.delete_po(conn, f"UNPARSED-{CHANNEL}-{uid}", channel=CHANNEL)
    store.mark_email_processed(conn, CHANNEL, uid, po_number)
    status = record.get("pipeline_status")
    notify_inbox(conn, po_number, status, checks["last_error"], previous, s3_key=s3_key, amend_status=amend_status)
    conn.commit()
    moved = bool(folder) and move_enabled() and mail_is_ready(status, checks["last_error"]) and move_to_processed(mail, uid, folder)
    return {"uid": uid, "po_number": po_number, "status": status, "last_error": checks["last_error"], "mail_moved": moved}


def run_fetch(max_messages: int | None = None) -> dict[str, Any]:
    """Read every new email in the Flipkart label and store its PO in RDS."""
    settings = load_mail_settings()
    code = card_code(settings)
    folder = mailbox_label(settings)
    mail = connect_mailbox(settings)
    _status, found = mail.uid("search", None, "ALL")
    uids = [u.decode() for u in (found[0] or b"").split()]
    if max_messages:
        uids = uids[-int(max_messages) :]

    results = []
    with store.connect() as conn:
        for uid in uids:
            if store.already_processed(conn, CHANNEL, uid):
                results.append({"uid": uid, "status": "skipped"})
                continue
            try:
                results.append(process_email(conn, mail, uid, code, folder))
            except Exception as exc:
                logger.exception("Flipkart UID %s failed", uid)
                conn.rollback()
                results.append({"uid": uid, "status": "error", "error": str(exc)})
        try:
            move_ready_mails(conn, mail, uids, folder)
        except Exception:
            logger.warning("Moving ready emails failed", exc_info=True)
    try:
        mail.logout()
    except Exception:
        pass
    return {"channel": CHANNEL, "processed": len(results), "results": results}


def rematch_po(conn, po_number: str) -> dict[str, Any]:
    """Dashboard 'recheck': re-run the checks on the saved Excel after the maps were fixed."""
    detail = store.get_po(conn, po_number, CHANNEL)
    if not detail:
        raise ValueError("PO not found")
    header = detail["header"]
    if header.get("pipeline_status") in {store.STATUS_POSTED, store.STATUS_CANCELLED}:
        raise ValueError("Posted POs cannot be rechecked")
    if not header.get("xlsx_s3_key"):
        raise ValueError("No Excel file stored for this PO")

    parsed = parse_excel(_read_from_s3(header["xlsx_s3_key"]))
    code = header.get("customer_code") or card_code(load_mail_settings())
    checks = run_checks(conn, parsed, code)
    record = save_po(
        conn, parsed, checks,
        code=code, s3_key=header["xlsx_s3_key"], uid=header.get("email_uid") or "", subject=header.get("email_subject") or "",
    )
    conn.commit()
    return {
        "ok": not checks["last_error"],
        "po_number": po_number,
        "pipeline_status": record.get("pipeline_status"),
        "last_error": checks["last_error"],
    }

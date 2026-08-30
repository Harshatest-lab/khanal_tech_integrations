"""Blinkit intake: IMAP + PDF/XLSX parse + address match + RDS/S3 (Frappe Blink.py)."""

from __future__ import annotations

import email
import imaplib
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from email.header import decode_header
from pathlib import Path
from typing import Any

import store
from hana_lookup import SapError, get_business_partner, get_items

logger = logging.getLogger(__name__)

_BLINK_MARKERS = (
    "HANDS ON TRADES PRIVATE LIMITED",
    "BLINK COMMERCE PRIVATE LIMITED",
    "Partners Biz Portal",
)
_EMAIL_TEMPLATE = Path(__file__).with_name("email_template.html")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# --- credentials ---

# SAP Business Partner for Blinkit. Does not change; not stored in SSM.
BLINKIT_CARD_CODE = "C03570"

# One shared mailbox; channel = Gmail label (folder). Not /b2b/blinkit/imap.
IMAP_PARAM_NAME = _env("B2B_IMAP_PARAM_NAME", "/b2b/imap")


@dataclass(frozen=True)
class ImapCredentials:
    email: str
    password: str
    imap_server: str
    imap_port: int
    folder: str


def get_imap_credentials() -> ImapCredentials:
    try:
        from credentials_local import IMAP  # type: ignore

        return ImapCredentials(
            email=IMAP["email"],
            password=IMAP["password"],
            imap_server=IMAP["imap_server"],
            imap_port=int(IMAP.get("imap_port") or 993),
            folder=_folder_from_imap_data(IMAP),
        )
    except ImportError:
        pass
    import boto3

    raw = boto3.client("ssm").get_parameter(Name=IMAP_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    data = json.loads(raw)
    creds = ImapCredentials(
        email=(data.get("email") or "").strip(),
        password=(data.get("password") or "").strip(),
        imap_server=(data.get("imap_server") or "").strip(),
        imap_port=int(data.get("imap_port") or 993),
        folder=_folder_from_imap_data(data),
    )
    if not (creds.email and creds.password and creds.imap_server):
        raise RuntimeError(f"SSM {IMAP_PARAM_NAME} is missing email/password/imap_server")
    return creds


def _folder_from_imap_data(data: dict) -> str:
    folders = data.get("folders") if isinstance(data.get("folders"), dict) else {}
    folder = str(folders.get("blinkit") or data.get("folder") or data.get("label_name") or "").strip()
    if not folder:
        raise RuntimeError(
            f"SSM {IMAP_PARAM_NAME} must set folder or folders.blinkit (Gmail label for this channel)"
        )
    return folder


# --- S3 ---

def _s3():
    import boto3

    return boto3.client("s3")


def _bucket() -> str:
    name = _env("B2B_DOCS_BUCKET")
    if not name:
        raise RuntimeError("B2B_DOCS_BUCKET is not set")
    return name


def put_bytes(key: str, body: bytes, content_type: str) -> str:
    _s3().put_object(Bucket=_bucket(), Key=key, Body=body, ContentType=content_type)
    return key


def blinkit_pdf_key(po_number: str) -> str:
    prefix = (_env("B2B_S3_PREFIX") or "blinkit").strip("/")
    return f"{prefix}/Blinkit_{po_number or 'Unknown_PO'}.pdf"


def blinkit_xlsx_key(po_number: str) -> str:
    prefix = (_env("B2B_S3_PREFIX") or "blinkit").strip("/")
    return f"{prefix}/Blinkit_{po_number or 'Unknown_PO'}.xlsx"


def public_url(key: str) -> str:
    """Stable public object URL. Does not expire (bucket allows public GetObject)."""
    if not key:
        return ""
    raw = str(key).strip()
    if raw.startswith(("http://", "https://")):
        return raw.split("?", 1)[0]
    try:
        bucket = _bucket()
    except Exception:
        return raw
    from urllib.parse import quote

    region = _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "ap-south-1"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{quote(raw.lstrip('/'), safe='/')}"


def presign(key: str, expires: int = 3600) -> str:
    return public_url(key)


# --- email ---

ADMIN_EMAIL = _env("B2B_ALERT_ADMIN_EMAIL")
FROM_EMAIL = _env("B2B_ALERT_FROM_EMAIL")
SES_REGION = _env("B2B_SES_REGION", "ap-south-1")


def _recipients() -> list[str]:
    return [addr.strip() for addr in ADMIN_EMAIL.split(",") if addr.strip()]


def send_alert(subject: str, body: str) -> bool:
    if not (ADMIN_EMAIL and FROM_EMAIL):
        logger.info("Alert email not configured; skipping: %s", subject)
        return False
    try:
        import boto3

        boto3.client("ses", region_name=SES_REGION).send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": _recipients()},
            Message={"Subject": {"Data": f"[B2B Blinkit] {subject}"}, "Body": {"Text": {"Data": body}}},
        )
        return True
    except Exception:
        logger.error("SES alert failed for %r", subject, exc_info=True)
        return False


def send_po_email(*, po_number: str, message: str, file_url: str = "", channel_name: str = "Blinkit") -> bool:
    if not (ADMIN_EMAIL and FROM_EMAIL):
        return False
    html = _EMAIL_TEMPLATE.read_text(encoding="utf-8") if _EMAIL_TEMPLATE.exists() else ""
    po_link = f'<a href="{file_url}" target="_blank" rel="noopener noreferrer">Open PDF</a>' if file_url else "N/A"
    html = (
        html.replace("{{ recipient_name }}", "Team")
        .replace("{{ po_number or \"N/A\" }}", str(po_number or "N/A"))
        .replace("{{ error_message or \"No details provided.\" }}", str(message or "No details provided."))
        .replace("{{ po_link_html }}", po_link)
        .replace("{{ channel_name }}", channel_name)
    )
    try:
        import boto3

        boto3.client("ses", region_name=SES_REGION).send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": _recipients()},
            Message={
                "Subject": {"Data": f"Notification from B2B {channel_name} Channel - PO Number: {po_number}"},
                "Body": {
                    "Text": {"Data": f"PO {po_number}\n\n{message}\n\n{file_url}"},
                    "Html": {"Data": html or message},
                },
            },
        )
        return True
    except Exception:
        logger.error("SES PO email failed for %s", po_number, exc_info=True)
        return False


# --- PDF / XLSX / address ---

def extract_pdf_text_pypdf(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if not reader.pages:
        return ""
    return "".join(page.extract_text() or "" for page in reader.pages)


def extract_pdf_text_plumber(pdf_bytes: bytes) -> str:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            piece = page.extract_text()
            if piece:
                parts.append(piece)
    return "\n".join(parts)


def _normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def extract_company_name(text: str) -> str:
    match = re.search(r"Purchase\s+Order\s*\r?\n\s*([A-Za-z][A-Za-z0-9 &().,\-]+)\s*\r?\n", text)
    if match:
        return _normalize_ws(match.group(1))
    match = re.search(r"Purchase\s+Order\s+([A-Za-z][A-Za-z0-9 &().,\-]+)\s*\r?\n", text)
    if match:
        return _normalize_ws(match.group(1))
    match = re.search(r"\r?\n\s*([A-Za-z][A-Za-z0-9 &().,\-]+)\s*\r?\n\s*PAN\s*:?", text, flags=re.IGNORECASE)
    if match:
        return _normalize_ws(match.group(1))
    match = re.search(r"\b([A-Za-z][A-Za-z0-9 &().,\-]+PRIVATE\s+LIMITED)\b", text[:3000], flags=re.IGNORECASE)
    if match:
        return _normalize_ws(match.group(1))
    for candidate in ("HANDS ON TRADES PRIVATE LIMITED", "BLINK COMMERCE PRIVATE LIMITED"):
        if candidate in text:
            return candidate
    return "Not Found"


def extract_delivered_to(text: str) -> str:
    match = re.search(
        r"Delivered\s*(?:\r?\n\s*)*To\s*:\s*(?P<name>[A-Za-z][A-Za-z0-9 &().,\-]+)\s*(?P<addr>[\s\S]*?)"
        r"(?:\r?\n\s*)?(?:GST\s*No\.?|Reference|#\s*Item)",
        text,
    )
    if not match:
        match = re.search(
            r"Delivered\s*:\s*(?P<name>[A-Za-z][A-Za-z0-9 &().,\-]+)\s*(?:GST\s*No\.?\s*:\s*[\w\d]+\s*)?"
            r"[\r\n]+\s*To\s*(?P<addr>[\s\S]*?)"
            r"(?:\r?\n\s*)?(?:GST\s*No\.?|Reference|#\s*Item)",
            text,
        )
    if not match:
        return "Not Found"
    name = _normalize_ws(match.group("name"))
    addr = re.sub(r"\n?\s*GST No\.\s*:\s*[\w\d]+\s*", " ", match.group("addr") or "")
    addr = re.sub(r"\n?\s*Reference\s*:.*", " ", addr)
    delivered_to = _normalize_ws(f"{name} {_normalize_ws(addr)}")
    if not re.search(r"\b\d{6}\b", delivered_to):
        pin = re.search(r"\b\d{6}\b", text)
        if pin:
            delivered_to = _normalize_ws(f"{delivered_to} {pin.group(0)}")
    return delivered_to


def extract_gst_no(text: str) -> str:
    delivered = re.search(
        r"Delivered\s*To\s*:\s*[\s\S]*?GST\s*No\.?\s*:\s*([0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3})",
        text,
        flags=re.IGNORECASE,
    )
    if delivered:
        return (delivered.group(1) or "").strip().upper()
    gst_matches = re.findall(r"GST\s*No\.?\s*:\s*([0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3})", text, flags=re.IGNORECASE)
    if not gst_matches:
        return "Not Found"
    return (gst_matches[1] if len(gst_matches) >= 2 else gst_matches[0]).strip().upper()


def is_valid_po_pdf_text(text: str) -> bool:
    if not text:
        return False
    has_blink = any(marker in text for marker in _BLINK_MARKERS)
    has_pan = re.search(r"\bPAN\s*:?\s*[A-Z]{5}\d{4}[A-Z]\b", text, flags=re.IGNORECASE) is not None
    has_gst = re.search(r"\bGST\s*No\.?\s*:?\s*[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}\b", text, flags=re.IGNORECASE) is not None
    if not (has_blink and (has_pan or has_gst)):
        return False
    has_po = re.search(r"P\.O\.\s*Number\s*:\s*\d+", text) is not None
    return bool(has_po and "Purchase Order" in text and re.search(r"Delivered\s*To\s*:", text))


def _parse_expiry(raw: str) -> str:
    cleaned = re.sub(r"\b(Sept|September)\b", "Sep", raw.replace(".", ""))
    for fmt in ("%B %d, %Y, %I:%M %p", "%b %d, %Y, %I:%M %p", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return "Invalid Date Format"


def extract_header(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    data["company_name"] = extract_company_name(text)
    if data["company_name"] == "HANDS ON TRADES PRIVATE LIMITED":
        data["employee_name"] = "Abhinay Pansari"
        data["employee_code"] = "103"
    contact = re.search(r"Contact Name:\s*([A-Za-z\s]+)", text)
    data["contact_name"] = re.sub(r"Phone No.*", "", contact.group(1)).strip() if contact else "Not Found"
    po = re.search(r"P\.O\.\s*Number\s*:\s*(\d+)", text)
    data["po_number"] = po.group(1) if po else "Not Found"
    expiry = re.search(
        r"PO expiry date\s*[:\-]?\s*([A-Za-z]{3,9}\.?[\s]\d{1,2},\s\d{4}(?:,\s*\d{1,2}:\d{2}\s*[ap]\.?m\.?)?)",
        text,
    )
    data["po_expiry_date"] = _parse_expiry(expiry.group(1)) if expiry else "Not Found"
    data["delivered_to"] = extract_delivered_to(text)
    data["gst_no"] = extract_gst_no(text)
    return data


def merge_headers(primary: dict[str, str], secondary: dict[str, str]) -> dict[str, str]:
    merged = dict(primary)
    for key, value in secondary.items():
        if merged.get(key) in (None, "Not Found"):
            merged[key] = value
    return merged


def extract_blinkit_header(pdf_bytes: bytes) -> tuple[dict[str, str], str]:
    text = extract_pdf_text_pypdf(pdf_bytes)
    plumber_header: dict[str, str] = {}
    if not is_valid_po_pdf_text(text):
        text = extract_pdf_text_plumber(pdf_bytes)
        if not is_valid_po_pdf_text(text):
            raise ValueError("Not a Blinkit PO PDF (missing required markers)")
        plumber_header = extract_header(text)
    header = merge_headers(extract_header(text), plumber_header)
    if header.get("company_name") in (None, "Not Found") or header.get("delivered_to") in (None, "Not Found"):
        plumber_text = extract_pdf_text_plumber(pdf_bytes)
        if plumber_text:
            header = merge_headers(header, extract_header(plumber_text))
    return header, text


def parse_xlsx(xlsx_bytes: bytes) -> list[dict[str, Any]]:
    import openpyxl

    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    sheet = workbook.active
    if sheet is None:
        return []
    header = [cell.value for cell in sheet[1]]
    try:
        idx = {
            "item_code": header.index("Item Code"),
            "quantity": header.index("Quantity"),
            "discount": header.index("Margin %"),
            "product_description": header.index("Product Description"),
            "basic_cost_price": header.index("Basic Cost Price"),
            "igst_percentage": header.index("IGST %"),
            "tax_amount": header.index("Tax Amount"),
            "landing_rate": header.index("Landing Rate"),
            "mrp": header.index("MRP"),
            "total_amount": header.index("Total Amount"),
        }
    except ValueError:
        return []
    needed = list(idx.values())
    rows: list[dict[str, Any]] = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        if any(row[i] is None for i in needed):
            continue
        rows.append({key: row[i] for key, i in idx.items()})
    return rows


def normalize_gstin(value: str) -> str:
    return re.sub(r"\s+", "", (value or "")).upper().strip()


AMEND_SUBJECT_KEYWORDS = ("amend", "amended", "amendment", "revised", "revise")


def is_amend_subject(subject: str) -> bool:
    """Bigbasket/Instamart-style gate: subject must mention an amend/revision."""
    text = (subject or "").lower()
    return any(keyword in text for keyword in AMEND_SUBJECT_KEYWORDS)


def skip_duplicate_without_amend(
    conn,
    *,
    uid: str,
    po_number: str,
    subject: str,
    existing_detail: dict[str, Any] | None,
) -> dict[str, Any]:
    """Duplicate PO email without amend keywords — do not update DB or mark mail processed."""
    message = (
        "Email subject does not contain 'Amend' or 'Revised'. "
        "Skipping duplicate PO update. Add Amend/Revised to the subject to replace this PO."
    )
    pdf_key = ((existing_detail or {}).get("header") or {}).get("pdf_s3_key") or ""
    file_url = presign(pdf_key) if pdf_key else ""
    store.add_notification(
        conn,
        kind="warning",
        title=f"Skipped duplicate PO {po_number}",
        body=message,
        po_number=po_number,
    )
    sent = send_po_email(po_number=po_number, message=message, file_url=file_url)
    store.add_email_log(
        conn,
        recipients=",".join(_recipients()),
        subject=f"Blinkit duplicate skipped {po_number}",
        po_number=po_number,
        sent_ok=sent,
    )
    conn.commit()
    logger.info("Skipped duplicate PO %s (uid %s) — subject lacks amend/revised: %r", po_number, uid, subject)
    return {
        "uid": uid,
        "po_number": po_number,
        "status": "skipped",
        "reason": "duplicate_without_amend_subject",
        "mail_moved": False,
    }


def match_ship_to(delivered_to: str, aliases: list[dict[str, str]]) -> dict[str, Any]:
    best = None
    best_score = 0.0
    for entry in aliases:
        haystack = entry.get("address_entry") or entry.get("address_entry") or ""
        score = SequenceMatcher(None, (delivered_to or "").lower(), haystack.lower()).ratio()
        if score > best_score:
            best_score = score
            best = entry
    if not best or best_score <= 0.6:
        return {"ok": False, "message": "No sufficiently similar address entry found."}
    return {
        "ok": True,
        "address_id": best.get("address_id") or best.get("address_id"),
        "address_entry": best.get("address_entry") or best.get("address_entry") or "",
        "score": best_score,
    }


match_ship_to = match_ship_to
match_ship_to = match_ship_to


def pick_sap_address(addresses: list[dict[str, Any]], address_id: str) -> dict[str, Any] | None:
    want = (address_id or "").strip()
    for address in addresses:
        name = str(address.get("AddressName") or address.get("AddressName") or "").strip()
        if name == want:
            return address
    return None


# --- IMAP pipeline ---

def _move_processed() -> bool:
    return _env("B2B_MOVE_PROCESSED_MAIL").lower() in {"1", "true", "yes"}


def _subject(msg: email.message.Message) -> str:
    bits: list[str] = []
    for piece, enc in decode_header(msg.get("Subject") or ""):
        bits.append(piece.decode(enc or "utf-8", errors="replace") if isinstance(piece, bytes) else piece)
    return " ".join(bits).strip()


def _attachments(msg: email.message.Message) -> tuple[bytes | None, bytes | None]:
    pdf = xlsx_bytes = None
    for part in msg.walk():
        name = (part.get_filename() or "").lower()
        payload = part.get_payload(decode=True)
        if not name or not isinstance(payload, bytes):
            continue
        if name.endswith(".pdf") and pdf is None:
            pdf = payload
        elif name.endswith(".xlsx") and xlsx_bytes is None:
            xlsx_bytes = payload
    return pdf, xlsx_bytes


def _processed_folder(source_folder: str) -> str:
    return f"{(source_folder or 'Blinkit').strip()}-Processed"


def _move_to_processed(mail: imaplib.IMAP4_SSL, uid: str, *, source_folder: str = "Blinkit") -> bool:
    dest = _processed_folder(source_folder)
    try:
        mail.create(f'"{dest}"')
    except imaplib.IMAP4.error:
        pass
    if mail.uid("copy", uid, f'"{dest}"')[0] != "OK":
        logger.warning("IMAP copy to %s failed for uid %s", dest, uid)
        return False
    if mail.uid("store", uid, "+FLAGS", r"(\Deleted)")[0] != "OK":
        logger.warning("IMAP delete flag failed for uid %s in %s", uid, source_folder)
        return False
    mail.expunge()
    logger.info("Moved email uid %s from %s to %s", uid, source_folder, dest)
    return True


def _parse_tax_rate(item: dict[str, Any]) -> int:
    """SAP Item Master U_TaxRate is the source of truth. Excel IGST % is not used."""
    raw = item.get("U_TaxRate")
    if raw not in (None, "", "N/A"):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            pass
    for key in ("U_IgstTax", "U_GstTax"):
        text = str(item.get(key) or "")
        found = re.search(r"(\d+)", text)
        if found:
            return int(found.group(1))
    return 0


def _taxcode(state: str, tax_rate: Any) -> str:
    """Karnataka (KT) → KACS{rate}; other states → KAIG{rate}."""
    prefix = "KACS" if (state or "").upper() == "KT" else "KAIG"
    try:
        rate = int(float(tax_rate or 0))
    except (TypeError, ValueError):
        rate = 0
    return f"{prefix}{rate}"


def enrich_lines_from_item_master(lines: list[dict[str, Any]], matched_state: str = "") -> list[dict[str, Any]]:
    """Fill tax_rate / GST / IGST / taxcode from OITM once a SAP item code exists."""
    codes = [str(r.get("sap_code") or "").strip() for r in lines if str(r.get("sap_code") or "").strip()]
    try:
        item_by_code = get_items(codes) if codes else {}
    except SapError:
        logger.exception("Item master lookup failed while filling tax codes")
        item_by_code = {}
    out: list[dict[str, Any]] = []
    for row in lines:
        rec = dict(row)
        sap = str(rec.get("sap_code") or "").strip()
        item = item_by_code.get(sap) or {}
        if item:
            tax_rate = _parse_tax_rate(item)
            rec["item_name"] = item.get("ItemName") or rec.get("item_name") or ""
            rec["tax_rate"] = tax_rate
            rec["gst_tax"] = item.get("U_GstTax") or rec.get("gst_tax") or ""
            rec["igst_tax"] = item.get("U_IgstTax") or rec.get("igst_tax") or ""
            rec["taxcode"] = _taxcode(matched_state, tax_rate)
        out.append(rec)
    return out


def unparsed_po_number(uid: str) -> str:
    return f"UNPARSED-{uid}"


def decide_pipeline(last_error: str, lines: list) -> str:
    if (last_error or "").strip():
        return store.STATUS_NEEDS_REVIEW
    if not lines:
        return store.STATUS_PARSED
    return store.STATUS_READY


def should_notify_po_update(prev_header: dict[str, Any] | None, *, pipeline_status: str, last_error: str) -> bool:
    """Skip duplicate inbox/email alerts when a resent PO email did not change status."""
    if not prev_header:
        return True
    prev_status = str(prev_header.get("pipeline_status") or "")
    prev_error = str(prev_header.get("last_error") or "").strip()
    return prev_status != pipeline_status or prev_error != (last_error or "").strip()


def rematch_po_state(
    delivered_to: str,
    lines: list[dict[str, Any]],
    aliases: list[dict[str, str]],
    sku_map: dict[str, str],
) -> dict[str, Any]:
    """Re-check ship-to + SKU maps on a stored PO without reading IMAP again."""
    match = match_ship_to(delivered_to or "", aliases)
    errors: list[str] = []
    matched_name = ""
    if not match.get("ok"):
        errors.append(match.get("message") or "No sufficiently similar address entry found.")
    else:
        matched_name = str(match.get("address_id") or "")
    next_lines: list[dict[str, Any]] = []
    missing: list[str] = []
    for line in lines:
        row = dict(line)
        item_code = str(row.get("item_code") or "").strip()
        sap_code = sku_map.get(item_code) or ""
        row["sap_code"] = sap_code
        if item_code and not sap_code:
            missing.append(item_code)
        next_lines.append(row)
    if missing:
        errors.append("No mapping found for item codes: " + ", ".join(missing))
    last_error = " ".join(errors).strip()
    return {
        "ok": not last_error,
        "last_error": last_error,
        "lines": next_lines,
        "matched_address_name": matched_name,
        "pipeline_status": decide_pipeline(last_error, next_lines),
    }


def get_bytes(key: str) -> bytes | None:
    if not key:
        return None
    try:
        return _s3().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    except Exception:
        logger.exception("Could not read s3://%s/%s", _bucket(), key)
        return None


def rematch_po(conn, po_number: str, channel: str = store.CHANNEL_BLINKIT) -> dict[str, Any]:
    detail = store.get_po(conn, po_number, channel)
    if not detail:
        raise ValueError("PO not found")
    header = dict(detail["header"])
    if header.get("pipeline_status") in {store.STATUS_POSTED, store.STATUS_CANCELLED}:
        raise ValueError("Posted POs cannot be rechecked")
    lines = [dict(r) for r in detail["lines"]]
    pdf_raw = get_bytes(header.get("pdf_s3_key") or "")
    if pdf_raw:
        try:
            parsed_header, _ = extract_blinkit_header(pdf_raw)
            for src, dest in (
                ("company_name", "company_name"),
                ("company_name", "company_name"),
                ("delivered_to", "delivered_to"),
                ("delivered_to", "delivered_to"),
                ("gst_no", "gst_no"),
                ("gst_no", "gst_no"),
                ("po_expiry_date", "po_expiry_date"),
                ("po_expiry_date", "po_expiry_date"),
            ):
                val = parsed_header.get(src)
                if val and val not in {"Not Found", "Invalid Date Format"}:
                    header[dest] = val
        except Exception:
            logger.exception("Could not re-read PDF for PO %s", po_number)
    xlsx_key = header.get("xlsx_s3_key") or ""
    raw = get_bytes(xlsx_key)
    if raw:
        parsed = parse_xlsx(raw)
        if parsed:
            lines = parsed
    state = rematch_po_state(
        header.get("delivered_to") or "",
        lines,
        store.list_ship_to_aliases(conn, channel),
        store.sku_map_dict(conn, channel),
    )
    header["last_error"] = state["last_error"]
    header["pipeline_status"] = state["pipeline_status"]
    if state["matched_address_name"]:
        header["matched_address_name"] = state["matched_address_name"]
        try:
            bp = get_business_partner(BLINKIT_CARD_CODE)
            sap_addr = pick_sap_address(bp.get("BPAddresses") or [], state["matched_address_name"])
            if sap_addr:
                header["matched_state"] = sap_addr.get("State") or header.get("matched_state") or ""
                header["bill_to"] = "Local" if (header.get("matched_state") or "").upper() == "KT" else "Central"
        except SapError:
            logger.exception("Could not load BP state while refetching PO %s", po_number)
    filled = enrich_lines_from_item_master(state["lines"], header.get("matched_state") or "")
    qty = sum(float(r.get("quantity") or 0) for r in filled)
    amt = sum(float(r.get("total_amount") or 0) for r in filled)
    header["total_quantity"] = qty
    header["total_amount"] = amt
    header["total_items"] = len({str(r.get("item_code")) for r in filled})
    store.upsert_purchase_order(conn, header, filled)
    return {
        "ok": state["ok"],
        "po_number": po_number,
        "pipeline_status": state["pipeline_status"],
        "last_error": state["last_error"],
        "message": "Refetched from stored PDF/Excel. PO is ready." if state["ok"] else state["last_error"],
    }


def record_intake_failure(
    conn,
    *,
    uid: str,
    subject: str,
    message: str,
    pdf: bytes | None = None,
) -> dict[str, Any]:
    """Keep a failed extract on the dashboard. Do not mark the mail processed."""
    po_number = unparsed_po_number(uid)
    pdf_key = ""
    if pdf:
        try:
            pdf_key = blinkit_pdf_key(po_number)
            put_bytes(pdf_key, pdf, "application/pdf")
        except Exception:
            logger.exception("Could not store failed PDF for UID %s", uid)
            pdf_key = ""
    existing = store.get_po(conn, po_number)
    store.upsert_purchase_order(
        conn,
        {
            "channel": store.CHANNEL_BLINKIT,
            "po_number": po_number,
            "pipeline_status": store.STATUS_FAILED,
            "company_name": "",
            "contact_name": "",
            "po_expiry_date": None,
            "delivered_to": "",
            "gst_no": "",
            "customer_code": BLINKIT_CARD_CODE,
            "salesperson_code": "",
            "matched_address_name": "",
            "matched_state": "",
            "billing_from": "KT",
            "bill_to": "",
            "email_uid": uid,
            "email_subject": subject or "",
            "pdf_s3_key": pdf_key,
            "xlsx_s3_key": "",
            "total_quantity": 0,
            "total_amount": 0,
            "total_items": 0,
            "last_error": message,
        },
        [],
    )
    prev_error = (existing or {}).get("header", {}).get("last_error") if existing else None
    if prev_error != message:
        store.add_notification(
            conn,
            kind="error",
            title="Could not extract purchase order",
            body=message,
            po_number=po_number,
        )
        sent = send_po_email(po_number=po_number, message=message, file_url=presign(pdf_key) if pdf_key else "")
        store.add_email_log(
            conn,
            recipients=",".join(_recipients()),
            subject=f"Blinkit extract failed {po_number}",
            po_number=po_number,
            sent_ok=sent,
        )
    conn.commit()
    return {"uid": uid, "po_number": po_number, "status": store.STATUS_FAILED, "error": message}


def process_one_email(
    conn,
    mail: imaplib.IMAP4_SSL,
    uid: str,
    *,
    source_folder: str = "Blinkit",
) -> dict[str, Any]:
    _ok, msg_data = mail.uid("fetch", uid, "(RFC822)")
    raw = None
    for part in msg_data or []:
        if isinstance(part, tuple) and isinstance(part[1], bytes):
            raw = part[1]
            break
    if not raw:
        return {"uid": uid, "status": "empty"}

    msg = email.message_from_bytes(raw)
    subject = _subject(msg)
    pdf, xlsx_bytes = _attachments(msg)
    if not pdf:
        return record_intake_failure(
            conn,
            uid=uid,
            subject=subject,
            message="This email has no PDF attachment, so the purchase order could not be extracted.",
        )

    try:
        header, _text = extract_blinkit_header(pdf)
    except Exception as exc:
        return record_intake_failure(
            conn,
            uid=uid,
            subject=subject,
            message=f"Could not extract this purchase order from the PDF. {exc}",
            pdf=pdf,
        )

    po_number = header.get("po_number") or "Unknown"
    if po_number in {"Unknown", "Not Found"}:
        return record_intake_failure(
            conn,
            uid=uid,
            subject=subject,
            message="Could not read a PO number from the PDF.",
            pdf=pdf,
        )
    existing_detail = store.get_po(conn, po_number, store.CHANNEL_BLINKIT)
    prev_header = (existing_detail or {}).get("header") if existing_detail else None
    amended = False
    amend_status = "False/0"
    if existing_detail:
        if not is_amend_subject(subject):
            logger.info(
                "Duplicate PO %s (uid %s) skipped — subject %r lacks amend/revised keywords",
                po_number,
                uid,
                subject,
            )
            return skip_duplicate_without_amend(
                conn,
                uid=uid,
                po_number=po_number,
                subject=subject,
                existing_detail=existing_detail,
            )
        logger.info("Amending PO %s (uid %s) — subject %r", po_number, uid, subject)
        amend_status = store.archive_po_for_amend(
            conn,
            existing_detail,
            email_subject=subject,
            email_uid=uid,
        )
        store.add_audit(
            conn,
            actor="fetch",
            action="amend_archive",
            po_number=po_number,
            detail=amend_status,
        )
        amended = True
        prev_header = None
    xlsx_rows = parse_xlsx(xlsx_bytes) if xlsx_bytes else []
    match = match_ship_to(header.get("delivered_to") or "", store.list_ship_to_aliases(conn))
    matched_name = ""
    matched_state = ""
    last_error = ""
    pipeline = store.STATUS_READY

    if not match["ok"]:
        pipeline = store.STATUS_NEEDS_REVIEW
        last_error = match["message"]
    else:
        try:
            bp = get_business_partner(BLINKIT_CARD_CODE)
        except SapError as exc:
            pipeline = store.STATUS_NEEDS_REVIEW
            last_error = str(exc)
            bp = {}
        sap_addr = pick_sap_address(bp.get("BPAddresses") or [], match.get("address_id") or "") if bp else None
        if not sap_addr:
            pipeline = store.STATUS_NEEDS_REVIEW
            last_error = last_error or f"No matching SAP address found for '{match.get('address_id')}'."
        else:
            matched_name = sap_addr.get("AddressName") or ""
            matched_state = sap_addr.get("State") or ""
            extracted_gst = normalize_gstin(header.get("gst_no") or "")
            sap_gst = normalize_gstin(
                sap_addr.get("GSTIN") or sap_addr.get("FederalTaxID") or bp.get("GSTIN") or bp.get("FederalTaxID")
            )
            if not extracted_gst or not sap_gst or extracted_gst != sap_gst:
                pipeline = store.STATUS_NEEDS_REVIEW
                last_error = f"GST mismatch. Extracted: {extracted_gst or 'Not Found'}, SAP: {sap_gst or 'Not Found'}."

    sku_map = store.sku_map_dict(conn)
    mapped_codes = [sku_map[str(r.get("item_code") or "")] for r in xlsx_rows if sku_map.get(str(r.get("item_code") or ""))]
    try:
        item_by_code = get_items(mapped_codes) if mapped_codes else {}
    except SapError as exc:
        pipeline = store.STATUS_NEEDS_REVIEW
        last_error = (last_error + " " if last_error else "") + str(exc)
        item_by_code = {}
    lines: list[dict[str, Any]] = []
    missing: list[str] = []
    for row in xlsx_rows:
        item_code = str(row.get("item_code") or "")
        sap_code = sku_map.get(item_code)
        if not sap_code:
            missing.append(item_code)
            lines.append(
                {
                    **row,
                    "sap_code": "",
                    "item_name": "",
                    "tax_rate": 0,
                    "gst_tax": "",
                    "igst_tax": "",
                    "taxcode": "",
                }
            )
            continue
        item = item_by_code.get(sap_code) or {}
        tax_rate = _parse_tax_rate(item)
        lines.append(
            {
                **row,
                "sap_code": sap_code,
                "item_name": item.get("ItemName") or "",
                "tax_rate": tax_rate,
                "gst_tax": item.get("U_GstTax") or "",
                "igst_tax": item.get("U_IgstTax") or "",
                "taxcode": _taxcode(matched_state, tax_rate),
            }
        )
    if missing:
        last_error = (last_error + " " if last_error else "") + f"No mapping found for item codes: {', '.join(missing)}"
    if not xlsx_bytes:
        last_error = (last_error + " " if last_error else "") + "No Excel (.xlsx) attachment found. Line items could not be extracted."
    elif not xlsx_rows:
        last_error = (last_error + " " if last_error else "") + "Excel attachment had no usable line items."
    pipeline = decide_pipeline(last_error, lines)

    pdf_key = blinkit_pdf_key(po_number)
    put_bytes(pdf_key, pdf, "application/pdf")
    xlsx_key = ""
    if xlsx_bytes:
        xlsx_key = blinkit_xlsx_key(po_number)
        put_bytes(xlsx_key, xlsx_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    qty = sum(float(r.get("quantity") or 0) for r in lines)
    amt = sum(float(r.get("total_amount") or 0) for r in lines)
    expiry = header.get("po_expiry_date") or header.get("po_expiry_date")
    record = store.upsert_purchase_order(
        conn,
        {
            "channel": store.CHANNEL_BLINKIT,
            "po_number": po_number,
            "pipeline_status": pipeline,
            "company_name": header.get("company_name") or header.get("company_name") or "",
            "contact_name": header.get("contact_name") or header.get("contact_name") or "",
            "po_expiry_date": expiry if expiry not in {"Not Found", "Invalid Date Format"} else None,
            "delivered_to": header.get("delivered_to") or header.get("delivered_to") or "",
            "gst_no": header.get("gst_no") or header.get("gst_no") or "",
            "customer_code": BLINKIT_CARD_CODE,
            "salesperson_code": "",
            "matched_address_name": matched_name,
            "matched_state": matched_state,
            "billing_from": "KT",
            "bill_to": "Local" if matched_state == "KT" else "Central",
            "email_uid": uid,
            "email_subject": subject,
            "pdf_s3_key": pdf_key,
            "xlsx_s3_key": xlsx_key,
            "total_quantity": qty,
            "total_amount": amt,
            "total_items": len({str(r.get("item_code")) for r in lines}),
            "last_error": last_error,
            "duplicate_records_status": amend_status if amended else "False/0",
        },
        lines,
        force_update=amended,
    )
    if record.get("skipped"):
        conn.rollback()
        reason = record.get("reason") or "update_blocked"
        message = (
            f"Could not update PO {po_number} after amend ({reason}). "
            "Deploy the latest fetch Lambda and retry."
        )
        logger.error(message)
        return {
            "uid": uid,
            "po_number": po_number,
            "status": "error",
            "reason": reason,
            "error": message,
            "mail_moved": False,
        }
    store.delete_po(conn, unparsed_po_number(uid))
    store.mark_email_processed(conn, store.CHANNEL_BLINKIT, uid, po_number)
    notify = amended or should_notify_po_update(prev_header, pipeline_status=pipeline, last_error=last_error)
    sent = False
    if notify:
        if amended:
            inbox_body = (
                "Email subject contains 'Amend' or 'Revised'. Proceeding to update records. "
                f"Status: {amend_status}."
            )
            inbox_title = f"Amended PO {po_number}"
            inbox_kind = "warning"
            email_message = inbox_body + (f" {last_error}" if last_error else "")
        else:
            inbox_body = last_error or f"Stored with status {pipeline}"
            inbox_title = ("Needs attention: PO " if last_error else "Stored: PO ") + po_number
            inbox_kind = "error" if last_error else "success"
            email_message = inbox_body
        store.add_notification(
            conn,
            kind=inbox_kind,
            title=inbox_title,
            body=inbox_body,
            po_number=po_number,
        )
        sent = send_po_email(po_number=po_number, message=email_message, file_url=presign(pdf_key))
        store.add_email_log(conn, recipients=",".join(_recipients()), subject=f"Blinkit PO {po_number}", po_number=po_number, sent_ok=sent)
    moved = False
    if _move_processed():
        moved = _move_to_processed(mail, uid, source_folder=source_folder)
    conn.commit()
    action = "amended" if amended else "created"
    return {
        "uid": uid,
        "po_number": po_number,
        "status": pipeline,
        "action": action,
        "amend_status": amend_status if amended else None,
        "id": record.get("id"),
        "mail_moved": moved,
    }


def run_fetch(max_messages: int | None = None) -> dict[str, Any]:
    creds = get_imap_credentials()
    mail = imaplib.IMAP4_SSL(creds.imap_server, creds.imap_port)
    mail.login(creds.email, creds.password)
    mail.select(f'"{creds.folder}"')
    _status, messages = mail.uid("search", None, "ALL")
    uids = (messages[0] or b"").split()
    if max_messages:
        uids = uids[-int(max_messages) :]
    results = []
    with store.connect() as conn:
        for raw_uid in uids:
            uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
            if store.already_processed(conn, store.CHANNEL_BLINKIT, uid):
                results.append({"uid": uid, "status": "skipped"})
                continue
            try:
                results.append(process_one_email(conn, mail, uid, source_folder=creds.folder))
            except Exception as exc:
                logger.exception("Blinkit UID %s failed", uid)
                conn.rollback()
                try:
                    results.append(record_intake_failure(conn, uid=uid, subject="", message=str(exc)))
                except Exception:
                    send_alert(f"Blinkit UID {uid} failed", str(exc))
                    results.append({"uid": uid, "status": "error", "error": str(exc)})
    try:
        mail.logout()
    except Exception:
        pass
    return {"processed": len(results), "results": results}

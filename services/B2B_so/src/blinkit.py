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
from gst_validate import extract_gst_from_text, normalize_gstin, validate_gst_match
from hana_lookup import SapError, get_business_partner, get_items
import intake_validate

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

# SAP Business Partner for Blinkit (default when card_codes.blinkit is not in SSM).
BLINKIT_CARD_CODE = "C03570"

CHANNEL_LABELS = {
    "blinkit": "Blinkit",
    "zepto": "Zepto",
    "instamart": "Instamart",
    "bigbasket": "Bigbasket",
    "more": "More",
}

# One shared mailbox; each channel = Gmail label under folders.{channel}.
IMAP_PARAM_NAME = _env("B2B_IMAP_PARAM_NAME", "/b2b/imap")
_imap_config_cache: dict[str, Any] | None = None


@dataclass(frozen=True)
class ImapCredentials:
    email: str
    password: str
    imap_server: str
    imap_port: int
    folder: str


def _load_imap_config() -> dict[str, Any]:
    global _imap_config_cache
    if _imap_config_cache is not None:
        return _imap_config_cache
    try:
        from credentials_local import IMAP  # type: ignore

        _imap_config_cache = dict(IMAP)
        return _imap_config_cache
    except ImportError:
        pass
    import boto3

    raw = boto3.client("ssm").get_parameter(Name=IMAP_PARAM_NAME, WithDecryption=True)["Parameter"]["Value"]
    _imap_config_cache = json.loads(raw)
    if not isinstance(_imap_config_cache, dict):
        raise RuntimeError(f"SSM {IMAP_PARAM_NAME} must be a JSON object")
    return _imap_config_cache


def channel_label(channel: str) -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    return CHANNEL_LABELS.get(ch, ch.replace("_", " ").title())


def folder_for_channel(channel: str) -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    if ch not in store.FETCH_CHANNELS:
        raise ValueError(f"Channel {ch!r} does not support mail fetch")
    data = _load_imap_config()
    folders = data.get("folders") if isinstance(data.get("folders"), dict) else {}
    folder = str(folders.get(ch) or "").strip()
    if not folder and ch == store.CHANNEL_BLINKIT:
        folder = str(data.get("folder") or data.get("label_name") or "").strip()
    if not folder:
        raise RuntimeError(
            f"SSM {IMAP_PARAM_NAME} must set folders.{ch} (Gmail label for {channel_label(ch)})"
        )
    return folder


def card_code_for_channel(channel: str) -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    data = _load_imap_config()
    codes = data.get("card_codes") if isinstance(data.get("card_codes"), dict) else {}
    code = str(codes.get(ch) or "").strip()
    if code:
        return code
    if ch == store.CHANNEL_BLINKIT:
        return BLINKIT_CARD_CODE
    raise RuntimeError(
        f"SSM {IMAP_PARAM_NAME} must set card_codes.{ch} (SAP Business Partner for {channel_label(ch)})"
    )


def list_fetch_channels() -> list[str]:
    data = _load_imap_config()
    folders = data.get("folders") if isinstance(data.get("folders"), dict) else {}
    if folders:
        return [ch for ch in store.FETCH_CHANNELS if str(folders.get(ch) or "").strip()]
    if str(data.get("folder") or data.get("label_name") or "").strip():
        return [store.CHANNEL_BLINKIT]
    return []


def get_imap_credentials(channel: str = store.CHANNEL_BLINKIT) -> ImapCredentials:
    data = _load_imap_config()
    creds = ImapCredentials(
        email=(data.get("email") or "").strip(),
        password=(data.get("password") or "").strip(),
        imap_server=(data.get("imap_server") or "").strip(),
        imap_port=int(data.get("imap_port") or 993),
        folder=folder_for_channel(channel),
    )
    if not (creds.email and creds.password and creds.imap_server):
        raise RuntimeError(f"SSM {IMAP_PARAM_NAME} is missing email/password/imap_server")
    return creds


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


def channel_pdf_key(channel: str, po_number: str) -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    label = channel_label(ch)
    return f"{ch}/{label}_{po_number or 'Unknown_PO'}.pdf"


def channel_xlsx_key(channel: str, po_number: str) -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    label = channel_label(ch)
    return f"{ch}/{label}_{po_number or 'Unknown_PO'}.xlsx"


def blinkit_pdf_key(po_number: str) -> str:
    return channel_pdf_key(store.CHANNEL_BLINKIT, po_number)


def blinkit_xlsx_key(po_number: str) -> str:
    return channel_xlsx_key(store.CHANNEL_BLINKIT, po_number)


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
    found = extract_gst_from_text(text)
    return found if found != "Not Found" else "Not Found"


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


def _apply_gst_validation(
    header: dict[str, str],
    *,
    sap_addr: dict[str, Any] | None,
    bp: dict[str, Any],
) -> str | None:
    """Compare extracted GST on the PO with SAP ship-to GST (HANA CRD1/OCRD)."""
    return validate_gst_match(header.get("gst_no") or "", sap_addr, bp)


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
    channel: str = store.CHANNEL_BLINKIT,
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
        channel=channel,
    )
    label = channel_label(channel)
    sent = send_po_email(po_number=po_number, message=message, file_url=file_url, channel_name=label)
    store.add_email_log(
        conn,
        recipients=",".join(_recipients()),
        subject=f"{label} duplicate skipped {po_number}",
        po_number=po_number,
        sent_ok=sent,
        channel=channel,
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


def _ship_to_match_candidates(delivered_to: str) -> list[str]:
    """Variants of PO ship-to text (Frappe compares multi-line warehouse block)."""
    text = (delivered_to or "").strip()
    if not text:
        return [""]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    candidates = [text]
    if lines:
        candidates.append("\n".join(lines))
        candidates.append(" ".join(lines))
        if lines[0].lower().strip() == "warehouse address":
            body = lines[1:]
            if body:
                candidates.append("\n".join(body))
                candidates.append(" ".join(body))
    seen: set[str] = set()
    out: list[str] = []
    for candidate in candidates:
        key = candidate.lower()
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def match_ship_to(delivered_to: str, aliases: list[dict[str, str]]) -> dict[str, Any]:
    best = None
    best_score = 0.0
    for candidate in _ship_to_match_candidates(delivered_to):
        for entry in aliases:
            haystack = entry.get("address_entry") or entry.get("address_entry") or ""
            score = SequenceMatcher(None, candidate.lower(), haystack.lower()).ratio()
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


def _attachments(msg: email.message.Message) -> tuple[bytes | None, bytes | None, str]:
    pdf = excel_bytes = None
    excel_name = ""
    for part in msg.walk():
        name = (part.get_filename() or "").lower()
        payload = part.get_payload(decode=True)
        if not name or not isinstance(payload, bytes):
            continue
        if name.endswith(".pdf") and pdf is None:
            pdf = payload
        elif name.endswith((".xlsx", ".xls", ".csv")) and excel_bytes is None:
            excel_bytes = payload
            excel_name = part.get_filename() or name
    return pdf, excel_bytes, excel_name


def _excel_storage_key(channel: str, po_number: str, filename: str = "") -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    label = channel_label(ch)
    name = (filename or "").lower()
    if name.endswith(".csv"):
        ext = "csv"
    elif name.endswith(".xls") and not name.endswith(".xlsx"):
        ext = "xls"
    else:
        ext = "xlsx"
    return f"{ch}/{label}_{po_number or 'Unknown_PO'}.{ext}"


def _excel_content_type(filename: str = "") -> str:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return "text/csv"
    if name.endswith(".xls") and not name.endswith(".xlsx"):
        return "application/vnd.ms-excel"
    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def extract_po_header(pdf_bytes: bytes, channel: str) -> tuple[dict[str, str], str]:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    if ch == "instamart":
        import instamart as instamart_parser

        return instamart_parser.extract_header(pdf_bytes)
    if ch == "zepto":
        import zepto as zepto_parser

        return zepto_parser.extract_header(pdf_bytes)
    if ch == "bigbasket":
        raise ValueError("Bigbasket purchase orders are extracted from Excel, not PDF")
    return extract_blinkit_header(pdf_bytes)


def parse_channel_lines(
    excel_bytes: bytes | None,
    *,
    channel: str,
    filename: str = "",
) -> list[dict[str, Any]]:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    if not excel_bytes:
        return []
    if ch == "instamart":
        import instamart as instamart_parser

        parsed = instamart_parser.parse_excel_attachment(excel_bytes, filename)
        return instamart_parser.lines_from_item_details(parsed.get("Item Details") or [])
    if ch == "zepto":
        import zepto as zepto_parser

        return zepto_parser.parse_attachment(excel_bytes).get("lines") or []
    if ch == "bigbasket":
        import bigbasket as bigbasket_parser

        return bigbasket_parser.parse_excel_attachment(excel_bytes, filename).get("lines") or []
    return parse_xlsx(excel_bytes)


def parse_channel_excel(excel_bytes: bytes, *, channel: str, filename: str = "") -> dict[str, Any]:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    if ch == "instamart":
        import instamart as instamart_parser

        return instamart_parser.parse_excel_attachment(excel_bytes, filename)
    if ch == "zepto":
        import zepto as zepto_parser

        return zepto_parser.parse_attachment(excel_bytes)
    if ch == "bigbasket":
        import bigbasket as bigbasket_parser

        return bigbasket_parser.parse_excel_attachment(excel_bytes, filename)
    return {"lines": parse_xlsx(excel_bytes)}


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


def _mail_ready_for_processed(pipeline_status: str, last_error: str) -> bool:
    """Only move mailbox mail once PO is ready for approval/posting (no blocking errors)."""
    return (pipeline_status or "") == store.STATUS_READY and not (last_error or "").strip()


def _move_ready_mails_in_folder(
    conn,
    mail: imaplib.IMAP4_SSL,
    *,
    channel: str,
    source_folder: str,
    uids: list[bytes | str],
) -> int:
    """Move inbox mails whose POs became ready after refetch (e.g. maps fixed on dashboard)."""
    if not _move_processed():
        return 0
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    moved = 0
    with conn.cursor() as cur:
        for raw_uid in uids:
            uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
            cur.execute(
                """
                SELECT po_number, pipeline_status, COALESCE(last_error, '')
                FROM b2b.purchase_orders
                WHERE channel = %s AND email_uid = %s
                LIMIT 1
                """,
                (ch, uid),
            )
            row = cur.fetchone()
            if not row or not _mail_ready_for_processed(row[1], row[2]):
                continue
            if _move_to_processed(mail, uid, source_folder=source_folder):
                moved += 1
                logger.info("Moved ready PO %s mail uid %s to %s-Processed", row[0], uid, source_folder)
    return moved


def move_ready_po_mail_for_header(header: dict[str, Any], *, channel: str) -> bool:
    """Move this PO's mailbox message to {Channel}-Processed once it is ready for approval."""
    if not _move_processed():
        return False
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    uid = str(header.get("email_uid") or "").strip()
    if not uid:
        logger.info("PO %s has no email_uid — skip processed-mail move", header.get("po_number") or "")
        return False
    if not _mail_ready_for_processed(header.get("pipeline_status"), header.get("last_error")):
        return False
    try:
        creds = get_imap_credentials(ch)
        mail = imaplib.IMAP4_SSL(creds.imap_server, creds.imap_port)
        mail.login(creds.email, creds.password)
        mail.select(f'"{creds.folder}"')
        moved = _move_to_processed(mail, uid, source_folder=creds.folder)
        try:
            mail.logout()
        except Exception:
            pass
        if moved:
            logger.info(
                "Moved ready PO %s mail uid %s from %s to %s-Processed",
                header.get("po_number"),
                uid,
                creds.folder,
                creds.folder,
            )
        return moved
    except Exception:
        logger.exception("Could not move processed mail for PO %s channel %s", header.get("po_number"), ch)
        return False


def _tax_rate_is_set(tax_rate: Any) -> bool:
    """True when rate is known, including explicit 0 (Nil GST in item master)."""
    if tax_rate is None or tax_rate == "":
        return False
    try:
        int(float(tax_rate))
        return True
    except (TypeError, ValueError):
        return False


def _parse_tax_rate(item: dict[str, Any]) -> int | None:
    """SAP Item Master U_TaxRate is the source of truth. Excel IGST % is not used."""
    if not item:
        return None
    raw = item.get("U_TaxRate")
    if raw is not None and raw != "" and str(raw).strip().upper() != "N/A":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            pass
    for key in ("U_IgstTax", "U_GstTax"):
        text = str(item.get(key) or "").strip()
        if not text or text.lower() in {"nil", "n/a", "na", "-"}:
            continue
        found = re.search(r"(\d+)", text)
        if found:
            return int(found.group(1))
    if item.get("ItemCode"):
        return 0
    return None


def _taxcode(state: str, tax_rate: Any) -> str:
    """Karnataka (KT) → KACS{rate}; other states → KAIG{rate}. Rate 0 → KAIG0 / KACS0 (Frappe)."""
    if not _tax_rate_is_set(tax_rate):
        return ""
    prefix = "KACS" if (state or "").upper() == "KT" else "KAIG"
    rate = int(float(tax_rate))
    return f"{prefix}{rate}"


def matched_state_for_tax(header: dict[str, Any]) -> str:
    state = str(header.get("matched_state") or "").strip()
    if state:
        return state
    if str(header.get("bill_to") or "").strip().lower() == "local":
        return "KT"
    return ""


def resolve_line_tax_code(
    line: dict[str, Any],
    *,
    matched_state: str = "",
    bill_to: str = "",
    header: dict[str, Any] | None = None,
) -> str:
    if header is not None:
        matched_state = matched_state or matched_state_for_tax(header)
        bill_to = bill_to or str(header.get("bill_to") or "")
    for key in ("taxcode", "tax_code", "TaxCode"):
        val = line.get(key)
        if val not in (None, ""):
            return str(val)
    rate = line.get("tax_rate")
    if _tax_rate_is_set(rate):
        if int(float(rate)) == 0 and not str(line.get("taxcode") or "").strip():
            gst_hint = str(line.get("gst_tax") or line.get("igst_tax") or "").strip().lower()
            if not gst_hint or gst_hint in {"n/a", "na", "-"}:
                return ""
        state = matched_state
        if not state and str(bill_to or "").strip().lower() == "local":
            state = "KT"
        return _taxcode(state, rate)
    return ""


def tax_code_gap_sap_codes(
    lines: list[dict[str, Any]],
    *,
    matched_state: str = "",
    bill_to: str = "",
    header: dict[str, Any] | None = None,
) -> list[str]:
    """SAP item codes mapped on the PO but missing a usable tax code."""
    if header is not None:
        matched_state = matched_state or matched_state_for_tax(header)
        bill_to = bill_to or str(header.get("bill_to") or "")
    gaps: list[str] = []
    for row in lines:
        sap = str(row.get("sap_code") or "").strip()
        if not sap:
            continue
        if not resolve_line_tax_code(row, matched_state=matched_state, bill_to=bill_to):
            gaps.append(sap)
    return gaps


def enrich_and_validate_tax_codes(header: dict[str, Any], lines: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Fill tax fields from OITM (HANA) and report SAP codes still missing TaxCode."""
    state = matched_state_for_tax(header)
    filled = enrich_lines_from_item_master(lines, state)
    gaps = tax_code_gap_sap_codes(filled, matched_state=state, bill_to=str(header.get("bill_to") or ""))
    if not gaps:
        return filled, ""
    return filled, f"Missing tax code from SAP item master (OITM / Service Layer) for: {', '.join(gaps)}"


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
            if tax_rate is not None:
                rec["tax_rate"] = tax_rate
                rec["gst_tax"] = item.get("U_GstTax") or rec.get("gst_tax") or ""
                rec["igst_tax"] = item.get("U_IgstTax") or rec.get("igst_tax") or ""
                rec["taxcode"] = _taxcode(matched_state, tax_rate)
        elif not rec.get("taxcode") and _tax_rate_is_set(rec.get("tax_rate")):
            rec["taxcode"] = _taxcode(matched_state, rec.get("tax_rate"))
        out.append(rec)
    return out


def unparsed_po_number(uid: str, channel: str = store.CHANNEL_BLINKIT) -> str:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    return f"UNPARSED-{ch}-{uid}"


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
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    detail = store.get_po(conn, po_number, ch)
    if not detail:
        raise ValueError("PO not found")
    header = dict(detail["header"])
    if header.get("pipeline_status") in {store.STATUS_POSTED, store.STATUS_CANCELLED}:
        raise ValueError("Posted POs cannot be rechecked")
    # Always use the SAP BP legal name for Bigbasket (old rows still say "Bigbasket").
    if ch == "bigbasket":
        import bigbasket as bigbasket_parser

        header["company_name"] = bigbasket_parser.BIGBASKET_CARD_NAME
    lines = [dict(r) for r in detail["lines"]]
    pdf_raw = get_bytes(header.get("pdf_s3_key") or "")
    pdf_text: str | None = None
    if pdf_raw and ch != "bigbasket":
        try:
            parsed_header, pdf_text = extract_po_header(pdf_raw, ch)
            for src, dest in (
                ("company_name", "company_name"),
                ("company_name", "company_name"),
                ("delivered_to", "delivered_to"),
                ("delivered_to", "delivered_to"),
                ("gst_no", "gst_no"),
                ("gst_no", "gst_no"),
            ):
                val = parsed_header.get(src)
                if val and val not in {"Not Found", "Invalid Date Format"}:
                    header[dest] = val
            if ch not in {"zepto"}:
                expiry = parsed_header.get("po_expiry_date")
                if expiry and expiry not in {"Not Found", "Invalid Date Format"}:
                    header["po_expiry_date"] = expiry
        except Exception:
            logger.exception("Could not re-read PDF for PO %s", po_number)
    xlsx_key = header.get("xlsx_s3_key") or ""
    raw = get_bytes(xlsx_key)
    excel_data: dict[str, Any] | None = None
    if raw:
        parsed = parse_channel_lines(raw, channel=ch, filename=xlsx_key)
        if parsed:
            lines = parsed
        if ch in {"instamart", "bigbasket", "zepto"}:
            try:
                excel_data = parse_channel_excel(raw, channel=ch, filename=xlsx_key)
                if ch == "instamart":
                    import instamart as instamart_parser

                    instamart_parser.apply_excel_to_header(header, excel_data)
                elif ch == "bigbasket":
                    import bigbasket as bigbasket_parser

                    header["company_name"] = str(
                        excel_data.get("company_name") or bigbasket_parser.BIGBASKET_CARD_NAME
                    )
                    main_address = excel_data.get("delivered_to") or excel_data.get("Warehouse Address") or ""
                    if main_address and main_address != "Not Found":
                        header["delivered_to"] = main_address
                    gst = str(excel_data.get("gst_no") or "")
                    if gst and gst not in {"Not Found", ""}:
                        header["gst_no"] = gst
                    expiry = str(excel_data.get("po_expiry_date") or "")
                    if expiry and expiry not in {"Not Found", "Invalid Date Format"}:
                        header["po_expiry_date"] = expiry
                else:
                    import zepto as zepto_parser

                    zepto_parser.apply_csv_to_header(header, excel_data, pdf_text=pdf_text)
            except Exception:
                logger.exception("Could not re-read %s Excel for PO %s", ch, po_number)
    intake_errors = intake_validate.validate_intake_data(ch, header, lines)
    if ch == "zepto" and excel_data:
        mismatch = intake_validate.zepto_pdf_csv_po_mismatch(header.get("po_number"), excel_data.get("po_numbers"))
        if mismatch:
            intake_errors.append(mismatch)
    state = rematch_po_state(
        header.get("delivered_to") or "",
        lines,
        store.list_ship_to_aliases(conn, ch),
        store.sku_map_dict(conn, ch),
    )
    header["last_error"] = intake_validate.join_errors(
        " ".join(intake_errors),
        state["last_error"],
    )
    header["pipeline_status"] = decide_pipeline(header["last_error"], state["lines"])
    if state["matched_address_name"]:
        try:
            bp = get_business_partner(header.get("customer_code") or card_code_for_channel(ch))
            sap_addr = pick_sap_address(bp.get("BPAddresses") or [], state["matched_address_name"])
            if sap_addr:
                header["matched_address_name"] = sap_addr.get("AddressName") or state["matched_address_name"]
                header["matched_state"] = sap_addr.get("State") or header.get("matched_state") or ""
                header["bill_to"] = "Local" if (header.get("matched_state") or "").upper() == "KT" else "Central"
                gst_error = _apply_gst_validation(header, sap_addr=sap_addr, bp=bp)
                if gst_error:
                    header["last_error"] = intake_validate.join_errors(header["last_error"], gst_error)
                    header["pipeline_status"] = store.STATUS_NEEDS_REVIEW
            else:
                header["matched_address_name"] = ""
                header["matched_state"] = ""
                msg = f"No matching SAP address found for '{state['matched_address_name']}'."
                header["last_error"] = intake_validate.join_errors(header["last_error"], msg)
                header["pipeline_status"] = store.STATUS_NEEDS_REVIEW
        except SapError as exc:
            header["matched_address_name"] = ""
            header["matched_state"] = ""
            header["last_error"] = intake_validate.join_errors(header["last_error"], str(exc))
            header["pipeline_status"] = store.STATUS_NEEDS_REVIEW
            logger.exception("Could not load BP state while refetching PO %s", po_number)
    else:
        header["matched_address_name"] = ""
        header["matched_state"] = ""
    filled, tax_err = enrich_and_validate_tax_codes(header, state["lines"])
    if tax_err:
        header["last_error"] = (header.get("last_error") + " " if header.get("last_error") else "") + tax_err
        header["pipeline_status"] = store.STATUS_NEEDS_REVIEW
    qty = sum(float(r.get("quantity") or 0) for r in filled)
    amt = sum(float(r.get("total_amount") or 0) for r in filled)
    header["total_quantity"] = qty
    header["total_amount"] = amt
    header["total_items"] = len({str(r.get("item_code")) for r in filled})
    store.upsert_purchase_order(conn, header, filled)
    mail_moved = move_ready_po_mail_for_header(header, channel=ch)
    ok = (header.get("pipeline_status") or "") == store.STATUS_READY and not (header.get("last_error") or "").strip()
    return {
        "ok": ok,
        "po_number": po_number,
        "pipeline_status": header["pipeline_status"],
        "last_error": header["last_error"],
        "mail_moved": mail_moved,
        "message": "Refetched from stored PDF/Excel. PO is ready." if ok else header["last_error"],
    }


def record_intake_failure(
    conn,
    *,
    uid: str,
    subject: str,
    message: str,
    pdf: bytes | None = None,
    channel: str = store.CHANNEL_BLINKIT,
) -> dict[str, Any]:
    """Keep a failed extract on the dashboard. Do not mark the mail processed."""
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    label = channel_label(ch)
    card_code = card_code_for_channel(ch)
    po_number = unparsed_po_number(uid, ch)
    pdf_key = ""
    if pdf:
        try:
            pdf_key = channel_pdf_key(ch, po_number)
            put_bytes(pdf_key, pdf, "application/pdf")
        except Exception:
            logger.exception("Could not store failed PDF for UID %s", uid)
            pdf_key = ""
    existing = store.get_po(conn, po_number, channel=ch)
    store.upsert_purchase_order(
        conn,
        {
            "channel": ch,
            "po_number": po_number,
            "pipeline_status": store.STATUS_FAILED,
            "company_name": "",
            "contact_name": "",
            "po_expiry_date": None,
            "delivered_to": "",
            "gst_no": "",
            "customer_code": card_code,
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
            channel=ch,
        )
        sent = send_po_email(po_number=po_number, message=message, file_url=presign(pdf_key) if pdf_key else "", channel_name=label)
        store.add_email_log(
            conn,
            recipients=",".join(_recipients()),
            subject=f"{label} extract failed {po_number}",
            po_number=po_number,
            sent_ok=sent,
            channel=ch,
        )
    conn.commit()
    return {"uid": uid, "po_number": po_number, "status": store.STATUS_FAILED, "error": message, "channel": ch}


def process_one_email(
    conn,
    mail: imaplib.IMAP4_SSL,
    uid: str,
    *,
    channel: str = store.CHANNEL_BLINKIT,
    source_folder: str = "Blinkit",
) -> dict[str, Any]:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    label = channel_label(ch)
    card_code = card_code_for_channel(ch)
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
    pdf, excel_bytes, excel_name = _attachments(msg)
    header: dict[str, str] = {}
    instamart_excel: dict[str, Any] = {}
    xlsx_rows: list[dict[str, Any]] = []

    if ch == "bigbasket":
        if not excel_bytes:
            return record_intake_failure(
                conn,
                uid=uid,
                subject=subject,
                message="This email has no Excel (.xls or .xlsx) attachment, so the Bigbasket purchase order could not be extracted.",
                channel=ch,
            )
        try:
            import bigbasket as bigbasket_parser

            parsed = bigbasket_parser.parse_excel_attachment(excel_bytes, excel_name)
            header = bigbasket_parser.header_from_parsed(parsed)
            xlsx_rows = parsed.get("lines") or []
        except Exception as exc:
            return record_intake_failure(
                conn,
                uid=uid,
                subject=subject,
                message=f"Could not extract this purchase order from the Excel file. {exc}",
                channel=ch,
            )
    else:
        if not pdf:
            return record_intake_failure(
                conn,
                uid=uid,
                subject=subject,
                message="This email has no PDF attachment, so the purchase order could not be extracted.",
                channel=ch,
            )
        try:
            header, pdf_text = extract_po_header(pdf, ch)
        except Exception as exc:
            return record_intake_failure(
                conn,
                uid=uid,
                subject=subject,
                message=f"Could not extract this purchase order from the PDF. {exc}",
                pdf=pdf,
                channel=ch,
            )

        if ch == "instamart":
            if not excel_bytes:
                return record_intake_failure(
                    conn,
                    uid=uid,
                    subject=subject,
                    message="No Excel (.xls or .xlsx) attachment found. Instamart line items could not be extracted.",
                    pdf=pdf,
                    channel=ch,
                )
            try:
                instamart_excel = parse_channel_excel(excel_bytes, channel=ch, filename=excel_name)
            except Exception as exc:
                return record_intake_failure(
                    conn,
                    uid=uid,
                    subject=subject,
                    message=f"Could not read the Instamart Excel attachment. {exc}",
                    pdf=pdf,
                    channel=ch,
                )
            import instamart as instamart_parser

            instamart_parser.apply_excel_to_header(header, instamart_excel)
        elif ch == "zepto":
            if not excel_bytes:
                return record_intake_failure(
                    conn,
                    uid=uid,
                    subject=subject,
                    message="No CSV attachment found. Zepto line items could not be extracted.",
                    pdf=pdf,
                    channel=ch,
                )
            try:
                zepto_excel = parse_channel_excel(excel_bytes, channel=ch, filename=excel_name)
            except Exception as exc:
                return record_intake_failure(
                    conn,
                    uid=uid,
                    subject=subject,
                    message=f"Could not read the Zepto CSV attachment. {exc}",
                    pdf=pdf,
                    channel=ch,
                )
            xlsx_rows = zepto_excel.get("lines") or []
            import zepto as zepto_parser

            zepto_parser.apply_csv_to_header(header, zepto_excel, pdf_text=pdf_text)
            mismatch = intake_validate.zepto_pdf_csv_po_mismatch(header.get("po_number"), zepto_excel.get("po_numbers"))
            if mismatch:
                return record_intake_failure(
                    conn,
                    uid=uid,
                    subject=subject,
                    message=mismatch,
                    pdf=pdf,
                    channel=ch,
                )

    po_number = header.get("po_number") or "Unknown"
    if po_number in {"Unknown", "Not Found"} and ch == "instamart":
        po_number = (instamart_excel.get("PO Details") or {}).get("PO No") or po_number
    if po_number in {"Unknown", "Not Found"}:
        return record_intake_failure(
            conn,
            uid=uid,
            subject=subject,
            message="Could not read a PO number from the PDF.",
            pdf=pdf,
            channel=ch,
        )
    existing_detail = store.get_po(conn, po_number, ch)
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
                channel=ch,
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
            channel=ch,
        )
        amended = True
        prev_header = None
    if ch not in {"bigbasket", "zepto"}:
        xlsx_rows = parse_channel_lines(excel_bytes, channel=ch, filename=excel_name) if excel_bytes else []
    match = match_ship_to(header.get("delivered_to") or "", store.list_ship_to_aliases(conn, ch))
    matched_name = ""
    matched_state = ""
    last_error = ""
    pipeline = store.STATUS_READY

    if not match["ok"]:
        pipeline = store.STATUS_NEEDS_REVIEW
        last_error = match["message"]
    else:
        try:
            bp = get_business_partner(card_code)
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
            gst_error = _apply_gst_validation(header, sap_addr=sap_addr, bp=bp)
            if gst_error:
                pipeline = store.STATUS_NEEDS_REVIEW
                last_error = gst_error

    sku_map = store.sku_map_dict(conn, ch)
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
                    "tax_rate": "",
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
                "tax_rate": tax_rate if tax_rate is not None else "",
                "gst_tax": item.get("U_GstTax") or "",
                "igst_tax": item.get("U_IgstTax") or "",
                "taxcode": _taxcode(matched_state, tax_rate) if tax_rate is not None else "",
            }
        )
    if missing:
        last_error = (last_error + " " if last_error else "") + f"No mapping found for item codes: {', '.join(missing)}"
    intake_err = " ".join(
        intake_validate.validate_intake_data(ch, header, xlsx_rows, skip_lines=(ch == "blinkit" and not excel_bytes))
    )
    if intake_err:
        last_error = intake_validate.join_errors(last_error, intake_err)
    if not excel_bytes:
        if ch == "zepto":
            excel_hint = "No CSV attachment found."
        elif ch in {"instamart", "bigbasket"}:
            excel_hint = "No Excel (.xls or .xlsx) attachment found."
        else:
            excel_hint = "No Excel (.xlsx) attachment found."
        last_error = (last_error + " " if last_error else "") + f"{excel_hint} Line items could not be extracted."
    elif not xlsx_rows and not intake_err:
        last_error = (last_error + " " if last_error else "") + "Line-item attachment had no usable rows."
    tax_gaps = tax_code_gap_sap_codes(lines, matched_state=matched_state, bill_to="Local" if matched_state == "KT" else "Central")
    if tax_gaps:
        last_error = (last_error + " " if last_error else "") + f"Missing tax code from SAP item master (OITM / Service Layer) for: {', '.join(tax_gaps)}"
    pipeline = decide_pipeline(last_error, lines)

    pdf_key = ""
    if pdf:
        pdf_key = channel_pdf_key(ch, po_number)
        put_bytes(pdf_key, pdf, "application/pdf")
    xlsx_key = ""
    if excel_bytes:
        xlsx_key = _excel_storage_key(ch, po_number, excel_name)
        put_bytes(xlsx_key, excel_bytes, _excel_content_type(excel_name))

    qty = sum(float(r.get("quantity") or 0) for r in lines)
    amt = sum(float(r.get("total_amount") or 0) for r in lines)
    expiry = header.get("po_expiry_date") or header.get("po_expiry_date")
    record = store.upsert_purchase_order(
        conn,
        {
            "channel": ch,
            "po_number": po_number,
            "pipeline_status": pipeline,
            "company_name": header.get("company_name") or header.get("company_name") or "",
            "contact_name": header.get("contact_name") or header.get("contact_name") or "",
            "po_expiry_date": expiry if expiry not in {"Not Found", "Invalid Date Format"} else None,
            "delivered_to": header.get("delivered_to") or header.get("delivered_to") or "",
            "gst_no": header.get("gst_no") or header.get("gst_no") or "",
            "customer_code": card_code,
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
    store.delete_po(conn, unparsed_po_number(uid, ch), channel=ch)
    store.mark_email_processed(conn, ch, uid, po_number)
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
            channel=ch,
        )
        sent = send_po_email(po_number=po_number, message=email_message, file_url=presign(pdf_key), channel_name=label)
        store.add_email_log(
            conn,
            recipients=",".join(_recipients()),
            subject=f"{label} PO {po_number}",
            po_number=po_number,
            sent_ok=sent,
            channel=ch,
        )
    moved = False
    if _move_processed() and _mail_ready_for_processed(pipeline, last_error):
        moved = _move_to_processed(mail, uid, source_folder=source_folder)
    elif _move_processed() and not _mail_ready_for_processed(pipeline, last_error):
        logger.info(
            "Keeping mail uid %s in %s — PO %s status=%s (not ready for posting)",
            uid,
            source_folder,
            po_number,
            pipeline,
        )
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
        "channel": ch,
    }


def run_fetch(channel: str = store.CHANNEL_BLINKIT, max_messages: int | None = None) -> dict[str, Any]:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    if ch not in store.FETCH_CHANNELS:
        raise ValueError(f"Channel {ch!r} does not support mail fetch")
    creds = get_imap_credentials(ch)
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
            if store.already_processed(conn, ch, uid):
                results.append({"uid": uid, "status": "skipped", "channel": ch})
                continue
            try:
                results.append(
                    process_one_email(conn, mail, uid, channel=ch, source_folder=creds.folder)
                )
            except Exception as exc:
                logger.exception("%s UID %s failed", channel_label(ch), uid)
                conn.rollback()
                try:
                    results.append(record_intake_failure(conn, uid=uid, subject="", message=str(exc), channel=ch))
                except Exception:
                    send_alert(f"{channel_label(ch)} UID {uid} failed", str(exc))
                    results.append({"uid": uid, "status": "error", "error": str(exc), "channel": ch})
        ready_moves = _move_ready_mails_in_folder(
            conn, mail, channel=ch, source_folder=creds.folder, uids=uids
        )
        if ready_moves:
            conn.commit()
            logger.info("%s fetch moved %s ready mail(s) to processed folder", channel_label(ch), ready_moves)
    try:
        mail.logout()
    except Exception:
        pass
    return {"channel": ch, "folder": creds.folder, "processed": len(results), "results": results}

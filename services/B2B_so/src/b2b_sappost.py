"""B2B SAP posting via Service Layer — create/update/cancel Sales Orders (all channels)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.parse import quote

from khanal_shared.sap_sl import build_opener_and_base_via_ssm, sap_sl_request_json
from khanal_shared.sap_sl.ssm_session import param_b1_session

import store

logger = logging.getLogger(__name__)


class SapError(Exception):
    pass


# Per-channel DocumentLines pricing (Frappe B2B *_SAP.py). Instamart: flip on with B2B_INSTAMART_SEND_PRICES=1.
_CHANNEL_LINE_PRICING: dict[str, dict[str, bool]] = {
    "blinkit": {"price": True, "unit_price": True},
    "bigbasket": {"price": True, "unit_price": True},
    "zepto": {"price": False, "unit_price": True},
    "instamart": {"price": False, "unit_price": False},
    "more": {"price": False, "unit_price": True},
}


def line_pricing_flags(channel: str) -> dict[str, bool]:
    ch = (channel or store.CHANNEL_BLINKIT).strip().lower()
    if ch == "instamart" and os.environ.get("B2B_INSTAMART_SEND_PRICES", "").strip().lower() in {"1", "true", "yes"}:
        return {"price": True, "unit_price": True}
    return dict(_CHANNEL_LINE_PRICING.get(ch, {"price": True, "unit_price": True}))


def _invalidate_session() -> None:
    import boto3

    try:
        ssm = boto3.client("ssm")
        name = param_b1_session()
        raw = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        data = json.loads(raw)
        data["expires_on"] = "1970-01-01T00:00:00+00:00"
        data["is_locked"] = False
        ssm.put_parameter(Name=name, Value=json.dumps(data), Type="SecureString", Overwrite=True)
    except Exception:
        logger.warning("Could not invalidate cached SAP session", exc_info=True)


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    opener, base = build_opener_and_base_via_ssm()
    status, resp = sap_sl_request_json(opener, method, f"{base}/{path}", body=body)
    if status == 401:
        _invalidate_session()
        opener, base = build_opener_and_base_via_ssm()
        return sap_sl_request_json(opener, method, f"{base}/{path}", body=body)
    return status, resp


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message") or {}
        if isinstance(msg, dict):
            return str(msg.get("value") or body)[:500]
        return str(msg or body)[:500]
    return str(body)[:500]


def find_order_by_num_at_card(num_at_card: str) -> dict[str, Any] | None:
    escaped = num_at_card.replace("'", "''")
    path = quote(
        f"Orders?$filter=NumAtCard eq '{escaped}' and Cancelled eq 'tNO'"
        "&$select=DocEntry,DocNum,DocumentStatus,NumAtCard&$top=5",
        safe="/?$&='",
    )
    status, body = _request("GET", path)
    if status != 200 or not isinstance(body, dict):
        raise SapError(f"Order lookup failed: HTTP {status}: {_error_text(body)}")
    rows = body.get("value") or []
    return rows[0] if rows else None


def create_order(payload: dict[str, Any]) -> dict[str, Any]:
    status, body = _request("POST", "Orders", body=payload)
    if status not in (200, 201) or not isinstance(body, dict) or not body.get("DocEntry"):
        raise SapError(f"SAP Create Order failed: HTTP {status}: {_error_text(body)}")
    return body


def update_order(doc_entry: int, payload: dict[str, Any]) -> None:
    status, body = _request("PATCH", f"Orders({doc_entry})", body=payload)
    if status not in (200, 204):
        raise SapError(f"SAP Update Order failed: HTTP {status}: {_error_text(body)}")


def get_order(doc_entry: int) -> dict[str, Any]:
    path = quote(
        f"Orders({int(doc_entry)})?$select=DocEntry,DocNum,DocumentStatus,NumAtCard,Cancelled,DocTotal,DocDate",
        safe="()/=$?",
    )
    status, body = _request("GET", path)
    if status != 200 or not isinstance(body, dict):
        raise SapError(f"SAP order fetch failed: HTTP {status}: {_error_text(body)}")
    return body


def cancel_order(doc_entry: int) -> dict[str, Any]:
    status, body = _request("POST", f"Orders({int(doc_entry)})/Cancel", body={})
    if status not in (200, 201, 204):
        raise SapError(f"SAP Cancel Order failed: HTTP {status}: {_error_text(body)}")
    return body if isinstance(body, dict) else {"http_status": status}


def is_open_document_status(status: str | None) -> bool:
    s = str(status or "").strip()
    if not s:
        return True
    low = s.lower()
    if low in {"cancelled", "bost_close", "closed", "bost_paid", "paid"}:
        return False
    return low in {"bost_open", "open"} or low.startswith("bost_open")


def _pick(data: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _slim_sap_response(body: dict[str, Any] | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    keys = (
        "DocEntry",
        "DocNum",
        "DocumentStatus",
        "NumAtCard",
        "DocDate",
        "DocDueDate",
        "CardCode",
        "CardName",
        "Cancelled",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if isinstance(body, dict) and key in body:
            out[key] = body.get(key)
    if extra:
        out.update(extra)
    return out


def _record_post_failure(
    conn,
    po_number: str,
    header: dict[str, Any],
    message: str,
    actor: str = "",
    channel: str = "",
) -> dict[str, Any]:
    ch = str(channel or header.get("channel") or store.CHANNEL_BLINKIT).strip().lower()
    current = header.get("pipeline_status") or store.STATUS_APPROVED
    if current == store.STATUS_POSTED:
        current = store.STATUS_APPROVED
    store.set_po_status(
        conn,
        po_number,
        current,
        channel=ch,
        sap_status="POST FAILED",
        last_error=message,
        sap_response={"status": "error", "message": message},
    )
    try:
        store.add_audit(conn, actor=actor, action="sap_post_failed", po_number=po_number, detail=message[:500], channel=ch)
    except Exception:
        logger.warning("Could not write sap_post_failed audit", exc_info=True)
    conn.commit()
    return {"status": "error", "message": message, "last_error": message}


def _finish_post(
    conn,
    po_number: str,
    *,
    actor: str,
    action: str,
    salesperson_code: str,
    sap_docentry: Any,
    sap_docnum: Any,
    sap_document_status: str,
    sap_response: dict[str, Any],
    message: str,
    channel: str = store.CHANNEL_BLINKIT,
    sap_doc_total: float | None = None,
    sap_doc_date: str | None = None,
) -> dict[str, Any]:
    store.set_po_status(
        conn,
        po_number,
        store.STATUS_POSTED,
        sap_status="SENT TO SAP",
        sap_docentry=int(sap_docentry),
        sap_docnum=int(sap_docnum or 0) if str(sap_docnum or "").isdigit() else sap_docnum,
        sap_document_status=sap_document_status,
        last_error="",
        sap_response=sap_response,
        channel=channel,
        sap_doc_total=sap_doc_total,
        sap_doc_date=sap_doc_date,
    )
    store.add_audit(conn, actor=actor, action=action, po_number=po_number, detail=str(sap_docentry), channel=channel)
    store.mark_notifications_read(conn, po_number=po_number, channel=channel)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE b2b.purchase_orders SET salesperson_code = %s WHERE channel = %s AND po_number = %s",
            (salesperson_code, channel, po_number),
        )
    conn.commit()
    return {
        "status": "success",
        "message": message,
        "docentry": sap_docentry,
        "docnum": sap_docnum,
        "sap_response": sap_response,
    }


def resolve_po_document_url(header: dict[str, Any]) -> str:
    """Public S3 URL for SAP U_PO_LINK — PDF first, then Excel/CSV (Bigbasket is Excel-only)."""
    import blinkit as _blinkit

    for key_field in ("pdf_s3_key", "xlsx_s3_key"):
        key = str(_pick(header, key_field, key_field) or "").strip()
        if key:
            url = _blinkit.public_url(key)
            if url:
                return url
    url = str(_pick(header, "po_url", "pdf_url", "xlsx_url") or "").strip()
    if "X-Amz-" in url:
        return url.split("?", 1)[0]
    return url


def po_link_error(header: dict[str, Any]) -> str:
    """Return a user-facing error when U_PO_LINK cannot be resolved, else empty string."""
    if resolve_po_document_url(header):
        return ""
    ch = str(header.get("channel") or "").strip().lower()
    if ch == "bigbasket":
        doc = "Excel file"
    elif ch in {"instamart", "zepto"}:
        doc = "PDF or Excel/CSV file"
    else:
        doc = "PDF file"
    return (
        f"PO document link (SAP U_PO_LINK) is empty — no {doc} is stored on this order. "
        "Click Refetch from mailbox."
    )


def _po_file_url(header: dict[str, Any]) -> str:
    return resolve_po_document_url(header)


def _matched_state_for_tax(header: dict[str, Any]) -> str:
    import blinkit as _blinkit

    return _blinkit.matched_state_for_tax(header)


def _resolve_line_tax_code(line: dict[str, Any], header: dict[str, Any]) -> str:
    import blinkit as _blinkit

    return _blinkit.resolve_line_tax_code(line, header=header)


def _lines_missing_tax_code(lines: list[dict[str, Any]], header: dict[str, Any]) -> list[str]:
    import blinkit as _blinkit

    return _blinkit.tax_code_gap_sap_codes(lines, header=header)


def _build_document_line(line: dict[str, Any], header: dict[str, Any], pricing: dict[str, bool]) -> dict[str, Any]:
    item_code = _pick(line, "sap_code", "sap_code")
    tax_code = _resolve_line_tax_code(line, header)
    row: dict[str, Any] = {
        "ItemCode": item_code,
        "Quantity": float(line.get("quantity") or 0),
        "TaxCode": tax_code,
    }
    price = float(_pick(line, "basic_cost_price", "basic_cost_price") or 0)
    if pricing["price"]:
        row["Price"] = price
    if pricing["unit_price"]:
        row["UnitPrice"] = price
    return row


def _stored_lines_tax_error(header: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    import blinkit as _blinkit

    gaps = _blinkit.tax_code_gap_sap_codes(lines, header=header)
    if not gaps:
        return ""
    return f"Missing tax code on PO lines for: {', '.join(gaps)}. Click Refetch first."


def _sap_post_precheck(header: dict[str, Any], payload: dict[str, Any]) -> str:
    """Block SAP create/update when mandatory header fields are empty."""
    due = str(payload.get("DocDueDate") or "").strip()
    if not due:
        return "PO expiry date (SAP DocDueDate) is empty. Set Doc due date and Save before posting."
    ship = str(payload.get("ShipToCode") or "").strip()
    if not ship:
        return "SAP ship-to is empty. Map the address and Refetch, or set SAP ship-to and Save before posting."
    lines = payload.get("DocumentLines") or []
    if not lines:
        return "PO has no SAP document lines to post."
    blank_items = [str(l.get("ItemCode") or "") for l in lines if not str(l.get("ItemCode") or "").strip()]
    if blank_items:
        return "One or more lines are missing SAP item codes."
    blank_tax = [str(l.get("ItemCode") or "") for l in lines if not str(l.get("TaxCode") or "").strip()]
    if blank_tax:
        return f"Missing TaxCode for SAP items: {', '.join(blank_tax)}"
    if not str(payload.get("U_PO_LINK") or "").strip():
        return po_link_error(header)
    return ""


# SAP numbering series for B2B sales orders: 514 = SO2026, valid for FY2026-27.
# SAP rejects a series from another financial year, so change this every April.
DEFAULT_ORDER_SERIES = 514


def order_series(channel: str) -> int:
    """Series for this channel: SSM /b2b/imap `series.<channel>` if set, else the hardcoded default."""
    import blinkit as _blinkit

    configured = (_blinkit._load_imap_config().get("series") or {}).get(channel)
    return int(configured) if configured else DEFAULT_ORDER_SERIES


def build_order_payload(
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    salesperson_code: str,
    *,
    skip_enrich: bool = False,
) -> dict[str, Any]:
    import blinkit as _blinkit

    if not skip_enrich:
        state = _matched_state_for_tax(header)
        lines = _blinkit.enrich_lines_from_item_master(lines, state)
    channel = str(_pick(header, "channel", "channel") or store.CHANNEL_BLINKIT).strip().lower()
    pricing = line_pricing_flags(channel)
    due = _pick(header, "po_expiry_date", "po_expiry_date")
    if str(due) in {"Not Found", "Invalid Date Format"}:
        due = ""
    sap_lines = []
    for line in lines:
        item_code = _pick(line, "sap_code", "sap_code")
        if not item_code:
            continue
        sap_lines.append(_build_document_line(line, header, pricing))
    card_name = _pick(header, "company_name", "company_name")
    if channel == "bigbasket":
        import bigbasket as _bigbasket

        card_name = _bigbasket.BIGBASKET_CARD_NAME
    return {
        "CardCode": _pick(header, "customer_code", "customer_code") or "C03570",
        "CardName": card_name,
        "Series": order_series(channel),
        "NumAtCard": _pick(header, "po_number", "po_number"),
        "DocDueDate": str(due or ""),
        "U_BillingFrom": _pick(header, "billing_from", "billing_from") or "KT",
        "U_BillTo": _pick(header, "bill_to", "bill_to"),
        "SalesPersonCode": salesperson_code,
        "ShipToCode": _pick(header, "matched_address_name", "matched_address_name"),
        "PayToCode": _pick(header, "matched_address_name", "matched_address_name"),
        "U_PO_LINK": _po_file_url(header),
        "DocumentLines": sap_lines,
    }


def post_purchase_order(
    conn,
    po_number: str,
    actor: str = "",
    salesperson_code: str = "",
    channel: str = store.CHANNEL_BLINKIT,
) -> dict[str, Any]:
    ch = str(channel or store.CHANNEL_BLINKIT).strip().lower()
    detail = store.get_po(conn, po_number, channel=ch)
    if not detail:
        return {"status": "error", "message": f"PO {po_number} not found"}
    header = detail["header"]
    lines = detail["lines"]
    channel = str(header.get("channel") or ch).strip().lower()
    if not lines:
        return _record_post_failure(conn, po_number, header, "PO has no lines", actor, channel=channel)
    if not salesperson_code:
        return _record_post_failure(conn, po_number, header, "Logged-in user has no SAP salesperson code", actor, channel=channel)
    header["po_url"] = resolve_po_document_url(header)
    try:
        tax_err = _stored_lines_tax_error(header, lines)
        if tax_err:
            return _record_post_failure(conn, po_number, header, tax_err, actor, channel=channel)
        payload = build_order_payload(header, lines, salesperson_code, skip_enrich=True)
        precheck = _sap_post_precheck(header, payload)
        if precheck:
            return _record_post_failure(conn, po_number, header, precheck, actor, channel=channel)
        blank_tax = [str(l.get("ItemCode") or "") for l in payload["DocumentLines"] if not str(l.get("TaxCode") or "").strip()]
        if blank_tax:
            msg = f"Missing TaxCode for SAP items: {', '.join(blank_tax)}"
            return _record_post_failure(conn, po_number, header, msg, actor, channel=channel)
        existing = find_order_by_num_at_card(po_number)
        # Frappe: PATCH only if this doc already has DocEntry. Do not PATCH just because
        # NumAtCard exists (Postman/create already posted it) — that sent DocDueDate into
        # a locked posting period.
        if header.get("sap_docentry"):
            doc_entry = int(header["sap_docentry"])
            update_order(doc_entry, {"DocumentLines": payload["DocumentLines"]})
            docnum = header.get("sap_docnum") or (existing or {}).get("DocNum")
            status_text = (existing or {}).get("DocumentStatus") or "Updated"
            sap_body = _slim_sap_response(
                existing,
                {"DocEntry": doc_entry, "DocNum": docnum, "DocumentStatus": status_text, "action": "updated"},
            )
            return _finish_post(
                conn,
                po_number,
                actor=actor,
                action="sap_update",
                salesperson_code=salesperson_code,
                sap_docentry=doc_entry,
                sap_docnum=docnum,
                sap_document_status=status_text,
                sap_response=sap_body,
                message=f"Updated SAP Order DocEntry {doc_entry} DocNum {docnum or '—'}",
                channel=channel,
            )
        if existing:
            doc_entry = int(existing["DocEntry"])
            docnum = existing.get("DocNum")
            sap_body = _slim_sap_response(existing, {"action": "already_posted"})
            return _finish_post(
                conn,
                po_number,
                actor=actor,
                action="sap_link",
                salesperson_code=salesperson_code,
                sap_docentry=doc_entry,
                sap_docnum=docnum,
                sap_document_status=existing.get("DocumentStatus") or "",
                sap_response=sap_body,
                message=f"Already in SAP as DocEntry {doc_entry} DocNum {docnum or '—'}",
                channel=channel,
            )
        created = create_order(payload)
        sap_body = _slim_sap_response(created, {"action": "created"})
        doc_total = float(created.get("DocTotal") or 0) if created.get("DocTotal") is not None else None
        return _finish_post(
            conn,
            po_number,
            actor=actor,
            action="sap_create",
            salesperson_code=salesperson_code,
            sap_docentry=created.get("DocEntry"),
            sap_docnum=created.get("DocNum"),
            sap_document_status=created.get("DocumentStatus") or "",
            sap_response=sap_body,
            message=f"Created SAP Order DocEntry {created.get('DocEntry')} DocNum {created.get('DocNum')}",
            channel=channel,
            sap_doc_total=doc_total,
            sap_doc_date=str(created.get("DocDate") or "")[:10] or None,
        )
    except Exception as exc:
        logger.exception("SAP post failed for PO %s", po_number)
        return _record_post_failure(conn, po_number, header, str(exc), actor, channel=channel)


def update_posted_order(
    conn,
    po_number: str,
    actor: str = "",
    salesperson_code: str = "",
    channel: str = store.CHANNEL_BLINKIT,
) -> dict[str, Any]:
    """PATCH an existing open SAP sales order from the current PO snapshot."""
    ch = str(channel or store.CHANNEL_BLINKIT).strip().lower()
    detail = store.get_po(conn, po_number, channel=ch)
    if not detail:
        return {"status": "error", "message": f"PO {po_number} not found"}
    header = detail["header"]
    lines = detail["lines"]
    channel = str(header.get("channel") or ch).strip().lower()
    if header.get("pipeline_status") != store.STATUS_POSTED:
        return {"status": "error", "message": "Only posted POs can be updated in SAP"}
    doc_entry = header.get("sap_docentry")
    if not doc_entry:
        return {"status": "error", "message": "This PO has no SAP DocEntry"}
    if not lines:
        return {"status": "error", "message": "PO has no lines"}
    if not salesperson_code:
        return {"status": "error", "message": "Logged-in user has no SAP salesperson code"}
    try:
        live = get_order(int(doc_entry))
        if not is_open_document_status(live.get("DocumentStatus")):
            return {"status": "error", "message": "SAP order is not open — update not allowed"}
        tax_err = _stored_lines_tax_error(header, lines)
        if tax_err:
            return {"status": "error", "message": tax_err, "last_error": tax_err}
        payload = build_order_payload(header, lines, salesperson_code, skip_enrich=True)
        blank_tax = [str(l.get("ItemCode") or "") for l in payload["DocumentLines"] if not str(l.get("TaxCode") or "").strip()]
        if blank_tax:
            msg = f"Missing TaxCode for SAP items: {', '.join(blank_tax)}"
            return {"status": "error", "message": msg, "last_error": msg}
        update_order(
            int(doc_entry),
            {"DocumentLines": payload["DocumentLines"], "DocDueDate": payload["DocDueDate"]},
        )
        refreshed = get_order(int(doc_entry))
        sap_body = _slim_sap_response(refreshed, {"action": "updated"})
        store.set_po_status(
            conn,
            po_number,
            store.STATUS_POSTED,
            sap_status="SENT TO SAP",
            sap_docentry=int(doc_entry),
            sap_docnum=refreshed.get("DocNum") or header.get("sap_docnum"),
            sap_document_status=str(refreshed.get("DocumentStatus") or header.get("sap_document_status") or ""),
            last_error="",
            sap_response=sap_body,
            channel=channel,
            sap_doc_total=float(refreshed["DocTotal"]) if refreshed.get("DocTotal") is not None else None,
            sap_doc_date=str(refreshed.get("DocDate") or "")[:10] or None,
        )
        store.add_audit(conn, actor=actor, action="sap_update", po_number=po_number, detail=str(doc_entry), channel=channel)
        conn.commit()
        return {
            "status": "success",
            "message": f"Updated SAP Order DocEntry {doc_entry} DocNum {refreshed.get('DocNum') or header.get('sap_docnum') or '—'}",
            "docentry": doc_entry,
            "docnum": refreshed.get("DocNum") or header.get("sap_docnum"),
            "sap_response": sap_body,
        }
    except Exception as exc:
        logger.exception("SAP update failed for PO %s", po_number)
        msg = str(exc)
        store.add_audit(conn, actor=actor, action="sap_update_failed", po_number=po_number, detail=msg[:500], channel=channel)
        conn.commit()
        return {"status": "error", "message": msg, "last_error": msg}


def cancel_posted_order(conn, po_number: str, actor: str = "", channel: str = store.CHANNEL_BLINKIT) -> dict[str, Any]:
    """Cancel an open SAP sales order via Service Layer."""
    ch = str(channel or store.CHANNEL_BLINKIT).strip().lower()
    detail = store.get_po(conn, po_number, channel=ch)
    if not detail:
        return {"status": "error", "message": f"PO {po_number} not found"}
    header = detail["header"]
    ch = str(header.get("channel") or ch).strip().lower()
    if header.get("pipeline_status") not in {store.STATUS_POSTED, store.STATUS_CANCELLED}:
        return {"status": "error", "message": "Only posted POs can be cancelled in SAP"}
    if header.get("pipeline_status") == store.STATUS_CANCELLED:
        return {"status": "success", "message": "PO is already cancelled in SAP"}
    doc_entry = header.get("sap_docentry")
    if not doc_entry:
        return {"status": "error", "message": "This PO has no SAP DocEntry"}
    try:
        live = get_order(int(doc_entry))
        if str(live.get("Cancelled") or "").lower() in {"tyes", "t", "y", "yes"}:
            store.set_po_status(
                conn,
                po_number,
                store.STATUS_CANCELLED,
                sap_document_status="cancelled",
                channel=ch,
            )
            conn.commit()
            return {"status": "success", "message": "PO is already cancelled in SAP"}
        if not is_open_document_status(live.get("DocumentStatus")):
            return {"status": "error", "message": "SAP order is not open — cancel not allowed"}
        sap_body = cancel_order(int(doc_entry))
        slim = _slim_sap_response(live, {"action": "cancelled", "cancel_response": sap_body})
        store.set_po_status(
            conn,
            po_number,
            store.STATUS_CANCELLED,
            sap_status="CANCELLED IN SAP",
            sap_document_status="cancelled",
            last_error="",
            sap_response=slim,
            channel=ch,
        )
        store.add_audit(conn, actor=actor, action="sap_cancel", po_number=po_number, detail=str(doc_entry), channel=ch)
        conn.commit()
        return {
            "status": "success",
            "message": f"Cancelled SAP Order DocEntry {doc_entry}",
            "docentry": doc_entry,
            "sap_response": slim,
        }
    except Exception as exc:
        logger.exception("SAP cancel failed for PO %s", po_number)
        msg = str(exc)
        store.add_audit(conn, actor=actor, action="sap_cancel_failed", po_number=po_number, detail=msg[:500], channel=ch)
        conn.commit()
        return {"status": "error", "message": msg, "last_error": msg}



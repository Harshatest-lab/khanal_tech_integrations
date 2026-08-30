"""Read live SAP sales order state from HANA (two-way sync for posted POs)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from hana_lookup import SapError, _schema, get_hana_params
from khanal_shared.hana import hana_connection, qualified_table

import store

logger = logging.getLogger(__name__)


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _as_date(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if "T" in text:
        return text.split("T", 1)[0]
    return text[:10] if len(text) >= 10 else text


def document_status(doc_status: str, canceled: str) -> str:
    if str(canceled or "").upper() in {"Y", "YES", "T"}:
        return "cancelled"
    code = str(doc_status or "").upper()
    if code == "C":
        return "bost_Close"
    if code == "O":
        return "bost_Open"
    return code or ""


def get_sales_orders(doc_entries: list[int]) -> dict[int, dict[str, Any]]:
    """ORDR rows keyed by DocEntry."""
    ids = [int(x) for x in doc_entries if x]
    if not ids:
        return {}
    if not get_hana_params():
        raise SapError("HANA is not configured (set HANA_USE_SSM=1 and /sap/hana_config).")
    ordr = qualified_table(_schema(), "ORDR", "T0")
    placeholders = ",".join("?" * len(ids))
    sql = f"""
        SELECT
          T0."DocEntry",
          T0."DocNum",
          T0."DocDate",
          T0."DocDueDate",
          T0."DocTotal",
          T0."DocStatus",
          T0."CANCELED",
          T0."NumAtCard",
          T0."CardCode",
          T0."CardName"
        FROM {ordr}
        WHERE T0."DocEntry" IN ({placeholders})
    """
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(ids))
        rows = _rows(cur)
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = int(row.get("DocEntry") or 0)
        if not entry:
            continue
        status = document_status(str(row.get("DocStatus") or ""), str(row.get("CANCELED") or ""))
        out[entry] = {
            "DocEntry": entry,
            "DocNum": row.get("DocNum"),
            "DocDate": _as_date(row.get("DocDate")),
            "DocDueDate": _as_date(row.get("DocDueDate")),
            "DocTotal": float(row.get("DocTotal") or 0),
            "DocumentStatus": status,
            "Cancelled": status == "cancelled",
            "NumAtCard": row.get("NumAtCard") or "",
            "CardCode": row.get("CardCode") or "",
            "CardName": row.get("CardName") or "",
        }
    return out


def sync_po_from_hana(conn, po_number: str, channel: str = store.CHANNEL_BLINKIT) -> dict[str, Any] | None:
    detail = store.get_po(conn, po_number, channel=channel)
    if not detail:
        return None
    header = detail["header"]
    docentry = header.get("sap_docentry")
    if not docentry or header.get("pipeline_status") not in {store.STATUS_POSTED, store.STATUS_CANCELLED}:
        return None
    try:
        live = get_sales_orders([int(docentry)]).get(int(docentry))
    except SapError as exc:
        logger.warning("HANA sync failed for PO %s: %s", po_number, exc)
        return None
    if not live:
        return None
    pipeline = store.STATUS_CANCELLED if live.get("Cancelled") else store.STATUS_POSTED
    store.set_po_status(
        conn,
        po_number,
        pipeline,
        sap_docentry=int(live["DocEntry"]),
        sap_docnum=int(live["DocNum"] or 0) if live.get("DocNum") is not None else header.get("sap_docnum"),
        sap_document_status=str(live.get("DocumentStatus") or ""),
        sap_doc_total=float(live.get("DocTotal") or 0),
        sap_doc_date=str(live.get("DocDate") or "") or None,
    )
    conn.commit()
    return live


def sync_posted_orders(conn, channel: str = store.CHANNEL_BLINKIT, limit: int = 200) -> int:
    rows = store.list_posted_for_sync(conn, channel=channel, limit=limit)
    entries = [int(r["sap_docentry"]) for r in rows if r.get("sap_docentry")]
    if not entries:
        return 0
    try:
        live_by_entry = get_sales_orders(entries)
    except SapError as exc:
        logger.warning("Bulk HANA sync failed: %s", exc)
        return 0
    updated = 0
    for row in rows:
        entry = int(row.get("sap_docentry") or 0)
        live = live_by_entry.get(entry)
        if not live:
            continue
        pipeline = store.STATUS_CANCELLED if live.get("Cancelled") else store.STATUS_POSTED
        store.set_po_status(
            conn,
            row["po_number"],
            pipeline,
            sap_docentry=entry,
            sap_docnum=int(live["DocNum"] or 0) if live.get("DocNum") is not None else row.get("sap_docnum"),
            sap_document_status=str(live.get("DocumentStatus") or ""),
            sap_doc_total=float(live.get("DocTotal") or 0),
            sap_doc_date=str(live.get("DocDate") or "") or None,
        )
        updated += 1
    if updated:
        conn.commit()
    return updated

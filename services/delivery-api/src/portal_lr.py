"""Portal LR verify, submit, SAP retry, and POD fix."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.types.json import Json

from src.delhivery_models import frappe_grn_key_from_delhivery_status
from src.portal_delhivery import (
    envelope_has_lr,
    fetch_delhivery_tracking,
    normalize_delhivery_track_envelope,
    tracking_summary,
)
from src.portal_invoice import PortalValidationError
from src.portal_sap import patch_audit_failure, patch_invoice_lr_track, patch_invoice_pod_fields
from src.portal_tracker import (
    _parse_sap_response_field,
    _sap_pod_posted_from_pod_response,
    _sap_synced_for_doc,
    fetch_delhivery_lr_row,
    fetch_sap_retry_candidates,
    row_needs_sap_retry,
)

UPSERT_TRACKING_SNAPSHOT_SQL = """
INSERT INTO delhivery_lr (
  lrnum, delhivery_status, raw_tracking, updated_at, created_at
) VALUES (
  %s, %s, %s::jsonb, now(), now()
)
ON CONFLICT (lrnum) DO UPDATE SET
  delhivery_status = EXCLUDED.delhivery_status,
  raw_tracking = EXCLUDED.raw_tracking,
  updated_at = now();
"""


def _clip_lrnum(raw: Any) -> str:
    s = (raw or "").strip()
    if not s:
        raise PortalValidationError("LR number is required.")
    if len(s) > 64:
        raise PortalValidationError("LR number is too long.")
    return s


def _status_from_webhook_payload(payload: Any) -> Optional[str]:
    if payload is None:
        return None
    p: Any = payload
    if isinstance(p, str) and p.strip():
        try:
            p = json.loads(p)
        except ValueError:
            return None
    if not isinstance(p, dict):
        return None
    for key in ("status", "Status", "STATUS"):
        v = p.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    data = p.get("data")
    if isinstance(data, dict):
        for key in ("status", "Status", "STATUS"):
            v = data.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
    return None


def _pod_date_from_raw_payload(raw: Any) -> Optional[str]:
    if isinstance(raw, dict):
        for k in ("PODDate", "podDate", "POD_DATE", "pod_date"):
            v = raw.get(k)
            if v is not None and str(v).strip():
                return str(v).strip()[:48]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return _pod_date_from_raw_payload(parsed) if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def save_tracking_snapshot_and_patch_lr(
    conn: Any,
    lrnum: str,
    tracking_raw: dict,
    doc_entry: int,
    doc_num: Optional[str],
    series: Optional[str],
    sap_response: Optional[dict] = None,
) -> tuple[dict, bool]:
    track_envelope = normalize_delhivery_track_envelope(tracking_raw, lrnum)
    summary = tracking_summary(track_envelope)
    status = None
    data = track_envelope.get("data")
    if isinstance(data, dict) and isinstance(data.get("status"), str):
        status = data.get("status")

    conn.execute(UPSERT_TRACKING_SNAPSHOT_SQL, (lrnum, status, Json(track_envelope)))

    if sap_response is not None:
        cur = conn.execute(
            """
            UPDATE delhivery_lr SET
                doc_entry = %s,
                doc_num = %s,
                series = %s,
                status_type = COALESCE(%s, status_type),
                shipment_remark = COALESCE(%s, shipment_remark),
                location = COALESCE(%s, location),
                waybill = COALESCE(%s, waybill),
                sap_response = %s::jsonb,
                updated_at = now()
            WHERE lrnum = %s
            """,
            (
                str(doc_entry),
                doc_num,
                series,
                summary.get("status_type"),
                summary.get("shipment_remark"),
                summary.get("location"),
                summary.get("waybill"),
                Json(sap_response),
                lrnum,
            ),
        )
    else:
        cur = conn.execute(
            """
            UPDATE delhivery_lr SET
                doc_entry = %s,
                doc_num = %s,
                series = %s,
                status_type = COALESCE(%s, status_type),
                shipment_remark = COALESCE(%s, shipment_remark),
                location = COALESCE(%s, location),
                waybill = COALESCE(%s, waybill),
                updated_at = now()
            WHERE lrnum = %s
            """,
            (
                str(doc_entry),
                doc_num,
                series,
                summary.get("status_type"),
                summary.get("shipment_remark"),
                summary.get("location"),
                summary.get("waybill"),
                lrnum,
            ),
        )
    patched = cur.rowcount > 0
    return {"ok": True, "lrnum": lrnum}, patched


def verify_lr(
    conn: Any,
    lrnum: str,
    doc_entry: Optional[int] = None,
    doc_num: Optional[str] = None,
    series: Optional[str] = None,
) -> dict[str, Any]:
    lr = _clip_lrnum(lrnum)
    tracking_raw = fetch_delhivery_tracking(lr)
    if not envelope_has_lr(tracking_raw, lr):
        raise PortalValidationError(f"Delhivery did not return tracking data for LR {lr}.")

    summary = tracking_summary(tracking_raw)
    snapshot_saved = False
    if doc_entry:
        de = int(doc_entry)
        doc_num_s = (str(doc_num).strip() if doc_num not in (None, "") else None) or None
        series_s = (str(series).strip() if series not in (None, "") else None) or None
        save_tracking_snapshot_and_patch_lr(
            conn, lr, tracking_raw, de, doc_num_s, series_s, sap_response=None
        )
        snapshot_saved = True

    return {
        "status": "ok",
        "lrnum": lr,
        "delhivery_status": summary.get("delhivery_status"),
        "waybill": summary.get("waybill"),
        "location": summary.get("location"),
        "shipment_remark": summary.get("shipment_remark"),
        "snapshot_saved_at_check_lr": snapshot_saved,
    }


def _tracking_from_saved_row(conn: Any, lr: str) -> Optional[dict[str, Any]]:
    """Reuse Check LR snapshot when present so Submit skips another Delhivery login+track."""
    row = fetch_delhivery_lr_row(conn, lr)
    if not row:
        return None
    raw = row.get("raw_tracking")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    if not envelope_has_lr(raw, lr):
        return None
    return raw


def submit_lr_and_sync_sap(
    conn: Any,
    doc_entry: int,
    lrnum: str,
    doc_num: Optional[str] = None,
    series: Optional[str] = None,
) -> dict[str, Any]:
    de = int(doc_entry)
    if not de:
        raise PortalValidationError("DocEntry is required.")
    lr = _clip_lrnum(lrnum)
    doc_num_s = (str(doc_num).strip() if doc_num not in (None, "") else None) or None
    series_s = (str(series).strip() if series not in (None, "") else None) or None

    tracking_raw = _tracking_from_saved_row(conn, lr)
    used_saved_snapshot = tracking_raw is not None
    if tracking_raw is None:
        tracking_raw = fetch_delhivery_tracking(lr)
    if not envelope_has_lr(tracking_raw, lr):
        raise PortalValidationError(f"Delhivery did not return tracking data for LR {lr}.")

    summary = tracking_summary(tracking_raw)
    _, postgres_patched = save_tracking_snapshot_and_patch_lr(
        conn, lr, tracking_raw, de, doc_num_s, series_s, sap_response=None
    )

    sap_error = None
    sap_payload = None
    sap_response = None
    sap_url = None
    try:
        sap_payload, sap_response, sap_url = patch_invoice_lr_track(de, lr)
    except PortalValidationError as exc:
        sap_error = str(exc)
        sap_response = patch_audit_failure(de, lr, sap_error, url=f"Invoices({de})")
        conn.execute(
            """
            UPDATE delhivery_lr SET sap_response = %s::jsonb, updated_at = now()
            WHERE lrnum = %s
            """,
            (Json(sap_response), lr),
        )
        postgres_patched = True

    if sap_error is None and sap_response is not None:
        save_tracking_snapshot_and_patch_lr(
            conn, lr, tracking_raw, de, doc_num_s, series_s, sap_response=sap_response
        )
        postgres_patched = True

    base = {
        "lrnum": lr,
        "doc_entry": de,
        "doc_num": doc_num_s,
        "series": series_s,
        "delhivery_status": summary.get("delhivery_status"),
        "waybill": summary.get("waybill"),
        "location": summary.get("location"),
        "shipment_remark": summary.get("shipment_remark"),
        "used_saved_snapshot": used_saved_snapshot,
        "sap_request": {
            "method": "PATCH",
            "url": sap_url,
            "payload": sap_payload or {"U_LRtrackno": lr},
        },
        "sap_response": sap_response,
        "sap_error": sap_error,
        "postgres_columns_patched": postgres_patched,
    }
    if sap_error:
        return {
            **base,
            "status": "partial",
            "message": f"Tracking saved for LR {lr}. SAP could not update DocEntry {de}: {sap_error}",
        }
    return {
        **base,
        "status": "success",
        "message": "LR saved to database and SAP invoice updated.",
    }


def portal_lr_row_sap_state(conn: Any, lrnum: str, doc_entry: int) -> dict[str, Any]:
    lr = _clip_lrnum(lrnum)
    de = int(doc_entry)
    row = fetch_delhivery_lr_row(conn, lr)
    if not row:
        return {"lr_found": False, "sap_failed_for_submit": False, "sap_error": None}

    try:
        row_de = int(row.get("doc_entry") or 0)
    except (TypeError, ValueError):
        row_de = 0
    if row_de != de:
        return {
            "lr_found": True,
            "doc_entry_match": False,
            "sap_failed_for_submit": False,
            "sap_error": None,
        }

    sap = _parse_sap_response_field(row.get("sap_response"))
    if not isinstance(sap, dict):
        return {
            "lr_found": True,
            "doc_entry_match": True,
            "sap_failed_for_submit": False,
            "sap_error": None,
        }

    failed = sap.get("ok") is False or sap.get("sap_sync_status") == "failed"
    patch = sap.get("patch") if isinstance(sap.get("patch"), dict) else {}
    patch_lr = (patch.get("U_LRtrackno") or "").strip() if patch else ""
    matches_lr = not patch_lr or patch_lr == lr
    sap_error = sap.get("error") if isinstance(sap.get("error"), str) else None

    return {
        "lr_found": True,
        "doc_entry_match": True,
        "sap_failed_for_submit": bool(failed and matches_lr),
        "sap_error": (sap_error or "").strip() or None,
    }


def retry_sap_sync_for_lr(conn: Any, lrnum: str) -> dict[str, Any]:
    lr = _clip_lrnum(lrnum)
    row = fetch_delhivery_lr_row(conn, lr)
    if not row:
        return {"status": "skipped", "lrnum": lr, "message": f"LR {lr} was not found in the database."}

    try:
        de = int(row.get("doc_entry") or 0)
    except (TypeError, ValueError):
        de = 0
    if not de:
        return {"status": "skipped", "lrnum": lr, "message": f"DocEntry is missing for LR {lr}."}

    doc_num_s = (str(row.get("doc_num")).strip() if row.get("doc_num") not in (None, "") else None) or None
    series_s = (str(row.get("series")).strip() if row.get("series") not in (None, "") else None) or None
    sap_existing = _parse_sap_response_field(row.get("sap_response"))

    if _sap_synced_for_doc(sap_existing, de):
        return {
            "status": "already_synced",
            "lrnum": lr,
            "doc_entry": de,
            "message": f"Already synced (DocEntry {de}).",
            "sap_response": sap_existing,
        }

    if not row_needs_sap_retry(row):
        return {
            "status": "skipped",
            "lrnum": lr,
            "doc_entry": de,
            "message": f"No SAP retry needed for LR {lr}.",
        }

    try:
        sap_payload, sap_response, sap_url = patch_invoice_lr_track(de, lr)
    except PortalValidationError as exc:
        sap_response = patch_audit_failure(de, lr, str(exc))
        conn.execute(
            """
            UPDATE delhivery_lr SET sap_response = %s::jsonb, updated_at = now()
            WHERE lrnum = %s
            """,
            (Json(sap_response), lr),
        )
        return {
            "status": "failed",
            "lrnum": lr,
            "doc_entry": de,
            "message": str(exc),
            "sap_response": sap_response,
        }

    conn.execute(
        """
        UPDATE delhivery_lr SET sap_response = %s::jsonb, updated_at = now()
        WHERE lrnum = %s
        """,
        (Json(sap_response), lr),
    )
    return {
        "status": "success",
        "lrnum": lr,
        "doc_entry": de,
        "doc_num": doc_num_s,
        "message": f"SAP updated (DocEntry {de}).",
        "sap_response": sap_response,
        "sap_request": {
            "method": "PATCH",
            "url": sap_url,
            "payload": sap_payload or {"U_LRtrackno": lr},
        },
    }


def retry_sap_sync_all_failed(conn: Any, max_rows: int = 50) -> dict[str, Any]:
    lim = min(max(int(max_rows or 50), 1), 100)
    candidates = fetch_sap_retry_candidates(conn, lim)
    if not candidates:
        return {
            "status": "done",
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "already_synced": 0,
            "message": "No records need SAP retry.",
            "results": [],
        }

    results: list[dict[str, Any]] = []
    counts = {"success": 0, "failed": 0, "skipped": 0, "already_synced": 0}
    for row in candidates:
        lr = row.get("lrnum") or ""
        try:
            item = retry_sap_sync_for_lr(conn, lr)
        except Exception as exc:
            item = {"status": "failed", "lrnum": lr, "message": str(exc)}
        st = item.get("status") or "failed"
        if st in counts:
            counts[st] += 1
        else:
            counts["failed"] += 1
        results.append(item)

    msg = f"Retried {len(results)} record(s): {counts['success']} succeeded, {counts['failed']} failed."
    return {
        "status": "done",
        "total": len(results),
        **counts,
        "message": msg,
        "results": results,
    }


def _fetch_latest_pod_row(conn: Any, lrnum: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT id, pod_url, raw_payload, sap_pod_response
        FROM delhivery_lr_pod
        WHERE lrnum = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (lrnum,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "pod_url": row[1],
        "raw_payload": row[2],
        "sap_pod_response": row[3],
    }


def _pod_url_from_raw(raw: Any) -> str:
    if isinstance(raw, dict):
        for k in ("pod_url", "podUrl", "POD_URL", "epod_url", "EPODUrl"):
            v = raw.get(k)
            if v and str(v).strip().startswith("http"):
                return str(v).strip()
    return ""


def fix_pod_sap_from_rds(conn: Any, lrnum: str) -> dict[str, Any]:
    lr = _clip_lrnum(lrnum)
    row = fetch_delhivery_lr_row(conn, lr)
    if not row:
        raise PortalValidationError("LR not found in database.")

    try:
        de = int(row.get("doc_entry") or 0)
    except (TypeError, ValueError):
        de = 0
    if not de:
        raise PortalValidationError(
            "DocEntry is missing on this LR. Submit the LR from Delivery first so the invoice is linked."
        )

    pod = _fetch_latest_pod_row(conn, lr)
    if not pod:
        raise PortalValidationError("No POD row in RDS for this LR.")
    if _sap_pod_posted_from_pod_response(pod.get("sap_pod_response")):
        return {"status": "already_ok", "message": "POD is already posted to SAP.", "lrnum": lr}

    pod_url = str(pod.get("pod_url") or "").strip() or _pod_url_from_raw(pod.get("raw_payload"))
    if not pod_url:
        raise PortalValidationError("No POD URL on the RDS row or in raw_payload.")

    pod_date = _pod_date_from_raw_payload(pod.get("raw_payload"))
    pod_id = int(pod.get("id") or 0)
    if not pod_id:
        raise PortalValidationError("Invalid POD row id.")

    resolved_status = _status_from_webhook_payload(row.get("last_status_webhook"))
    if not resolved_status and row.get("delhivery_status"):
        resolved_status = str(row.get("delhivery_status")).strip()
    grn_key = frappe_grn_key_from_delhivery_status(resolved_status)

    try:
        sap_payload, sap_body, _url = patch_invoice_pod_fields(
            de, lr, pod_url, pod_date, frappe_grn_key=grn_key
        )
    except PortalValidationError as exc:
        audit = {
            "ok": False,
            "lrnum": lr,
            "phase": "patch",
            "doc_entry": de,
            "error": str(exc)[:2000],
            "source": "portal_tracker_fix_pod",
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        conn.execute(
            "UPDATE delhivery_lr_pod SET sap_pod_response = %s::jsonb WHERE id = %s",
            (Json(audit), pod_id),
        )
        raise

    http_st = sap_body.get("http_status") if isinstance(sap_body, dict) else None
    try:
        hs = int(http_st) if http_st is not None else 204
    except (TypeError, ValueError):
        hs = 204

    audit = {
        "ok": True,
        "lrnum": lr,
        "phase": "patch",
        "doc_entry": de,
        "http_status": hs,
        "has_pod_date": bool(pod_date),
        "source": "portal_tracker_fix_pod",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "patch_keys": list(sap_payload.keys()) if isinstance(sap_payload, dict) else [],
    }
    conn.execute(
        "UPDATE delhivery_lr_pod SET sap_pod_response = %s::jsonb WHERE id = %s",
        (Json(audit), pod_id),
    )

    return {
        "status": "success",
        "lrnum": lr,
        "doc_entry": de,
        "message": f"POD fields sent to SAP for DocEntry {de}.",
        "sap_pod_response": audit,
        "u_frappe_grn_key": grn_key,
    }

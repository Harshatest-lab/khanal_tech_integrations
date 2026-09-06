"""Tracker report queries — delhivery_lr (+ pod lateral join)."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from src.portal_delhivery_parse import tracking_summary

_COLUMNS = [
    "lrnum",
    "delhivery_status",
    "status_type",
    "shipment_remark",
    "location",
    "waybill",
    "doc_entry",
    "doc_num",
    "series",
    "updated_at",
    "created_at",
    "raw_tracking",
    "sap_response",
    "last_status_webhook",
    "latest_pod_id",
    "pod_received",
]

_LR_ADMIN_COLUMNS = list(_COLUMNS) + ["test"]

_POD_EVENT_LIMIT = 500
_EPOD_B64_MAX_RETURN = 120_000


def _serialize(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return v
    return v


def _parse_sap_response_field(sap: Any) -> Optional[dict]:
    if isinstance(sap, dict):
        return sap
    if isinstance(sap, str) and sap.strip():
        try:
            parsed = json.loads(sap)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def _sap_pod_posted_from_pod_response(sap_pod: Any) -> bool:
    d = _parse_sap_response_field(sap_pod)
    if not d or d.get("ok") is not True:
        return False
    if str(d.get("phase") or "").lower() != "patch":
        return False
    try:
        h = int(d.get("http_status"))
    except (TypeError, ValueError):
        return False
    return h in (200, 204)


def lr_row_expects_pod_sap(row: dict) -> bool:
    pr = row.get("pod_received")
    if pr is True or pr == 1:
        return True
    if isinstance(pr, str) and pr.strip().lower() == "true":
        return True
    lip = row.get("latest_pod_id")
    if lip is None:
        return False
    s = str(lip).strip()
    return bool(s) and s != "0"


def _enrich_tracker_row_from_raw(rec: dict[str, Any]) -> None:
    """Fill waybill/location/remark from saved Delhivery JSON when column is empty."""
    raw = rec.get("raw_tracking")
    if not raw:
        return
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return
    if not isinstance(raw, dict):
        return
    summary = tracking_summary(raw)
    for key in ("waybill", "location", "shipment_remark"):
        if not rec.get(key) and summary.get(key):
            rec[key] = summary[key]
    if not rec.get("delhivery_status") and summary.get("delhivery_status"):
        rec["delhivery_status"] = summary["delhivery_status"]


def _finalize_tracker_record(rec: dict[str, Any], sap_latest: Any, latest_pod_url: Any) -> dict[str, Any]:
    _enrich_tracker_row_from_raw(rec)
    _compute_sap_pod_tracker_columns(rec, sap_latest, latest_pod_url)
    rec["needs_sap_retry"] = row_needs_sap_retry(rec)
    return rec


def _compute_sap_pod_tracker_columns(
    rec: dict,
    sap_latest: Any,
    latest_pod_url: Any,
) -> None:
    posted = _sap_pod_posted_from_pod_response(sap_latest)
    rec["sap_pod_posted"] = posted
    url_s = (str(latest_pod_url).strip() if latest_pod_url not in (None, "") else "") or ""
    # Only treat as "has POD" when we have a real POD URL or an explicit POD pointer —
    # not merely DocEntry + empty lateral join.
    has_pod = bool(url_s) or lr_row_expects_pod_sap(rec)
    try:
        de = int(rec.get("doc_entry") or 0)
    except (TypeError, ValueError):
        de = 0

    if not has_pod:
        rec["sap_pod_display"] = "none"
        rec["sap_pod_fixable"] = False
        return
    if posted:
        rec["sap_pod_display"] = "posted"
        rec["sap_pod_fixable"] = False
        return
    if de and url_s:
        rec["sap_pod_display"] = "failed_fixable"
        rec["sap_pod_fixable"] = True
        return
    if de and not url_s:
        # POD flagged on LR but file/URL not stored yet (webhook in progress).
        rec["sap_pod_display"] = "pending"
        rec["sap_pod_fixable"] = False
        return
    rec["sap_pod_display"] = "failed_blocked"
    rec["sap_pod_fixable"] = False


def _sap_synced_for_doc(sap: Optional[dict], doc_entry: int) -> bool:
    if not isinstance(sap, dict):
        return False
    if sap.get("ok") is not True and sap.get("sap_sync_status") != "synced":
        return False
    stored_de = sap.get("doc_entry")
    if stored_de is not None and int(stored_de) == doc_entry:
        return True
    url = sap.get("url") or ""
    return bool(url and f"Invoices({doc_entry})" in str(url))


def row_needs_sap_retry(row: dict) -> bool:
    try:
        de = int(row.get("doc_entry") or 0)
    except (TypeError, ValueError):
        de = 0
    if not de:
        return False
    sap = _parse_sap_response_field(row.get("sap_response"))
    if not sap:
        return True
    if sap.get("ok") is False or sap.get("sap_sync_status") == "failed":
        return True
    if _sap_synced_for_doc(sap, de):
        return False
    stored_de = sap.get("doc_entry")
    if stored_de is not None and int(stored_de) != de:
        return True
    m = re.search(r"Invoices\((\d+)\)", str(sap.get("url") or ""), re.I)
    if m and int(m.group(1)) != de:
        return True
    return False


def _row_dict_from_columns(row: tuple, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    rec = {c: _serialize(row[i]) for i, c in enumerate(_COLUMNS)}
    if extra:
        rec.update(extra)
    return rec


def get_tracker_records(
    conn: Any,
    *,
    lrnum: Optional[str] = None,
    doc_entry: Optional[str] = None,
    doc_num: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    pg = max(int(page or 1), 1)
    ps = min(max(int(page_size or 50), 1), 200)
    offset = (pg - 1) * ps

    where: list[str] = []
    params: list[Any] = []

    if lrnum and str(lrnum).strip():
        where.append("drlr.lrnum ILIKE %s")
        params.append("%" + str(lrnum).strip() + "%")
    if doc_entry and str(doc_entry).strip():
        where.append("drlr.doc_entry = %s")
        params.append(str(doc_entry).strip())
    if doc_num and str(doc_num).strip():
        where.append("drlr.doc_num = %s")
        params.append(str(doc_num).strip())
    if status and str(status).strip():
        where.append("drlr.delhivery_status ILIKE %s")
        params.append("%" + str(status).strip() + "%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    col_sql = ", ".join(f'"{c}"' for c in _COLUMNS)

    sql_with_pod = f"""
        SELECT {col_sql},
               lp.sap_pod_response AS sap_pod_response_latest,
               lp.pod_url AS latest_pod_url_latest
        FROM delhivery_lr drlr
        LEFT JOIN LATERAL (
            SELECT p.sap_pod_response, p.pod_url
            FROM delhivery_lr_pod p
            WHERE p.lrnum = drlr.lrnum
            ORDER BY p.id DESC
            LIMIT 1
        ) lp ON TRUE
        {where_sql}
        ORDER BY drlr.updated_at DESC
        LIMIT %s OFFSET %s
    """
    sql_lr_only = f"""
        SELECT {col_sql}
        FROM delhivery_lr drlr
        {where_sql}
        ORDER BY drlr.updated_at DESC
        LIMIT %s OFFSET %s
    """
    count_sql = f"SELECT COUNT(*) FROM delhivery_lr drlr {where_sql}"

    total = conn.execute(count_sql, tuple(params)).fetchone()[0]

    rows: list[tuple] = []
    try:
        rows = conn.execute(sql_with_pod, tuple(params + [ps, offset])).fetchall()
        has_pod_cols = True
    except Exception as exc:
        if "delhivery_lr_pod" not in str(exc).lower():
            raise
        rows = conn.execute(sql_lr_only, tuple(params + [ps, offset])).fetchall()
        has_pod_cols = False

    records: list[dict[str, Any]] = []
    for row in rows:
        if has_pod_cols:
            base = dict(zip(_COLUMNS + ["sap_pod_response_latest", "latest_pod_url_latest"], row))
            sap_latest = base.pop("sap_pod_response_latest", None)
            latest_pod_url = base.pop("latest_pod_url_latest", None)
            rec = {c: _serialize(base.get(c)) for c in _COLUMNS}
        else:
            rec = {c: _serialize(row[i]) for i, c in enumerate(_COLUMNS)}
            sap_latest = None
            latest_pod_url = None
        _finalize_tracker_record(rec, sap_latest, latest_pod_url)
        records.append(rec)

    total_int = int(total)
    return {
        "records": records,
        "total": total_int,
        "page": pg,
        "page_size": ps,
        "pages": max(1, -(-total_int // ps)),
    }


def fetch_sap_retry_candidates(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
    lim = min(max(int(limit or 50), 1), 100)
    col_sql = ", ".join(f'"{c}"' for c in _COLUMNS)
    cur = conn.execute(
        f"""
        SELECT {col_sql}
        FROM delhivery_lr
        WHERE doc_entry IS NOT NULL
          AND TRIM(COALESCE(doc_entry::text, '')) <> ''
        ORDER BY updated_at DESC
        LIMIT 500
        """
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        rec = {c: _serialize(row[i]) for i, c in enumerate(_COLUMNS)}
        if row_needs_sap_retry(rec):
            out.append(rec)
            if len(out) >= lim:
                break
    return out


def count_sap_retry_pending(conn: Any) -> dict[str, int]:
    return {"count": len(fetch_sap_retry_candidates(conn, 100))}


def get_logistics_report_alerts(conn: Any, limit: int = 35) -> dict[str, Any]:
    lim_out = min(max(int(limit or 35), 1), 80)
    scan_limit = min(500, max(120, lim_out * 18))
    col_sql = ", ".join(f'"{c}"' for c in _COLUMNS)

    sql_with_pod = f"""
        SELECT {col_sql},
               lp.sap_pod_response AS sap_pod_response_latest,
               lp.pod_url AS latest_pod_url_latest
        FROM delhivery_lr drlr
        LEFT JOIN LATERAL (
            SELECT p.sap_pod_response, p.pod_url
            FROM delhivery_lr_pod p
            WHERE p.lrnum = drlr.lrnum
            ORDER BY p.id DESC
            LIMIT 1
        ) lp ON TRUE
        ORDER BY drlr.updated_at DESC
        LIMIT %s
    """
    sql_lr_only = f"""
        SELECT {col_sql}
        FROM delhivery_lr drlr
        ORDER BY drlr.updated_at DESC
        LIMIT %s
    """

    try:
        rows = conn.execute(sql_with_pod, (scan_limit,)).fetchall()
        has_pod = True
    except Exception as exc:
        if "delhivery_lr_pod" not in str(exc).lower():
            raise
        rows = conn.execute(sql_lr_only, (scan_limit,)).fetchall()
        has_pod = False

    alerts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for row in rows:
        if len(alerts) >= lim_out:
            break
        if has_pod:
            base = dict(zip(_COLUMNS + ["sap_pod_response_latest", "latest_pod_url_latest"], row))
            sap_latest = base.pop("sap_pod_response_latest", None)
            latest_pod_url = base.pop("latest_pod_url_latest", None)
            rec = {c: _serialize(base.get(c)) for c in _COLUMNS}
        else:
            rec = {c: _serialize(row[i]) for i, c in enumerate(_COLUMNS)}
            sap_latest = None
            latest_pod_url = None
        _compute_sap_pod_tracker_columns(rec, sap_latest, latest_pod_url)

        lr = str(rec.get("lrnum") or "").strip()
        if not lr:
            continue
        try:
            de = int(rec.get("doc_entry") or 0)
        except (TypeError, ValueError):
            de = 0
        up = rec.get("updated_at")

        if de and row_needs_sap_retry(rec):
            key = (lr, "sap_track")
            if key not in seen:
                seen.add(key)
                alerts.append(
                    {
                        "lrnum": lr,
                        "kind": "sap_track",
                        "severity": "warning",
                        "title": "SAP LR tracking",
                        "message": f"LR {lr}: SAP invoice tracking is failed, stale, or not synced.",
                        "updated_at": _serialize(up),
                    }
                )

        pod_d = rec.get("sap_pod_display") or "none"
        if pod_d == "failed_fixable":
            key = (lr, "sap_pod_fix")
            if key not in seen and len(alerts) < lim_out:
                seen.add(key)
                alerts.append(
                    {
                        "lrnum": lr,
                        "kind": "sap_pod_fix",
                        "severity": "warning",
                        "title": "SAP POD",
                        "message": f"LR {lr}: POD is not posted to SAP — use Fix it on the tracker.",
                        "updated_at": _serialize(up),
                    }
                )
        elif pod_d == "failed_blocked":
            key = (lr, "sap_pod_blocked")
            if key not in seen and len(alerts) < lim_out:
                seen.add(key)
                alerts.append(
                    {
                        "lrnum": lr,
                        "kind": "sap_pod_blocked",
                        "severity": "danger",
                        "title": "SAP POD",
                        "message": f"LR {lr}: POD received but DocEntry is missing — complete delivery submit first.",
                        "updated_at": _serialize(up),
                    }
                )

    return {"alerts": alerts, "count": len(alerts)}


def _serialize_pod_admin_row(row: tuple) -> dict[str, Any]:
    keys = [
        "id",
        "lrnum",
        "waybill",
        "order_id",
        "epod_b64",
        "pod_url",
        "pod_format",
        "raw_payload",
        "sap_pod_response",
        "received_at",
    ]
    out: dict[str, Any] = {k: _serialize(row[i]) for i, k in enumerate(keys)}
    b64 = out.get("epod_b64")
    if isinstance(b64, str) and b64.strip():
        raw_len = len(b64)
        if raw_len > _EPOD_B64_MAX_RETURN:
            out["epod_b64"] = b64[:_EPOD_B64_MAX_RETURN]
            out["epod_b64_truncated"] = True
            out["epod_b64_total_length"] = raw_len
        else:
            out["epod_b64_truncated"] = False
    else:
        out["epod_b64"] = None
        out["epod_b64_truncated"] = False
    return out


def get_tracker_lr_admin_detail(conn: Any, lrnum: str) -> dict[str, Any]:
    lr = (lrnum or "").strip()
    if not lr:
        raise ValueError("LR number is required")

    admin_cols = list(_LR_ADMIN_COLUMNS)
    lr_row: Optional[dict[str, Any]] = None
    while admin_cols:
        col_sql = ", ".join(f'"{c}"' for c in admin_cols)
        try:
            row = conn.execute(
                f"SELECT {col_sql} FROM delhivery_lr WHERE lrnum = %s LIMIT 1",
                (lr,),
            ).fetchone()
            if not row:
                return {
                    "lr": None,
                    "status_events": [],
                    "pods": [],
                    "sap_pod_posted": False,
                    "sap_pod_display": "none",
                    "sap_pod_fixable": False,
                    "warnings": ["No row in delhivery_lr for this LR."],
                }
            lr_row = {c: _serialize(row[i]) for i, c in enumerate(admin_cols)}
            break
        except Exception as exc:
            msg = str(exc).lower()
            if "test" in admin_cols and "test" in msg and "column" in msg:
                admin_cols = [c for c in admin_cols if c != "test"]
                continue
            raise

    status_events: list[dict[str, Any]] = []
    pods: list[dict[str, Any]] = []
    warnings: list[str] = []

    try:
        for ev in conn.execute(
            """
            SELECT id, lrnum, payload, received_at
            FROM delhivery_lr_status_events
            WHERE lrnum = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (lr, _POD_EVENT_LIMIT),
        ).fetchall():
            status_events.append(
                {
                    "id": _serialize(ev[0]),
                    "lrnum": _serialize(ev[1]),
                    "payload": _serialize(ev[2]),
                    "received_at": _serialize(ev[3]),
                }
            )
    except Exception as exc:
        if "delhivery_lr_status_events" in str(exc).lower():
            warnings.append("Table delhivery_lr_status_events is missing; status history is unavailable.")
        else:
            raise

    try:
        for pod in conn.execute(
            """
            SELECT id, lrnum, waybill, order_id, epod_b64, pod_url, pod_format, raw_payload,
                   sap_pod_response, received_at
            FROM delhivery_lr_pod
            WHERE lrnum = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (lr, _POD_EVENT_LIMIT),
        ).fetchall():
            pods.append(_serialize_pod_admin_row(pod))
    except Exception as exc:
        if "delhivery_lr_pod" in str(exc).lower():
            warnings.append("Table delhivery_lr_pod is missing; POD history is unavailable.")
        else:
            raise

    tmp_disp = dict(lr_row) if lr_row else {}
    if lr_row:
        _enrich_tracker_row_from_raw(lr_row)
    sap_latest_ad = pods[0].get("sap_pod_response") if pods else None
    pod_url_ad = pods[0].get("pod_url") if pods else None
    _compute_sap_pod_tracker_columns(tmp_disp, sap_latest_ad, pod_url_ad)

    return {
        "lr": lr_row,
        "status_events": status_events,
        "pods": pods,
        "sap_pod_posted": bool(tmp_disp.get("sap_pod_posted")),
        "sap_pod_display": tmp_disp.get("sap_pod_display") or "none",
        "sap_pod_fixable": bool(tmp_disp.get("sap_pod_fixable")),
        "warnings": warnings,
    }


def fetch_delhivery_lr_row(conn: Any, lrnum: str) -> Optional[dict[str, Any]]:
    lr = (lrnum or "").strip()
    if not lr:
        return None
    col_sql = ", ".join(f'"{c}"' for c in _COLUMNS)
    row = conn.execute(
        f"SELECT {col_sql} FROM delhivery_lr WHERE lrnum = %s LIMIT 1",
        (lr,),
    ).fetchone()
    if not row:
        return None
    return {c: _serialize(row[i]) for i, c in enumerate(_COLUMNS)}

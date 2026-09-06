"""HTTP API handler for logistics portal (Cognito JWT + delhivery_lr)."""

from __future__ import annotations

import logging
import traceback

from khanal_shared.db.pool import ensure_warmed_for_lambda, get_pool

from src.portal_auth import PERM_EDIT, PERM_VIEW, ensure_portal_schema, portal_access
from src.portal_http import body, json_response, method, path, query
from src.portal_invoice import PortalValidationError, get_ar_invoice_detail, search_ar_invoices
from src.portal_lr import (
    fix_pod_sap_from_rds,
    portal_lr_row_sap_state,
    retry_sap_sync_all_failed,
    retry_sap_sync_for_lr,
    submit_lr_and_sync_sap,
    verify_lr,
)
from src.portal_tracker import (
    count_sap_retry_pending,
    get_logistics_report_alerts,
    get_tracker_lr_admin_detail,
    get_tracker_records,
)

logger = logging.getLogger(__name__)


def portal_handler(event, context):
    ensure_warmed_for_lambda()

    if method(event) == "OPTIONS":
        return json_response(200, {"ok": True})

    p = path(event)
    q = query(event)
    m = method(event)
    b = body(event)

    pool = get_pool()
    if pool is None:
        return json_response(503, {"error": "DATABASE_URL is not configured on delivery-api."})

    try:
        with pool.connection() as conn:
            conn.execute("SET search_path TO b2b, public")
            ensure_portal_schema(conn)

            if p.endswith("/v1/delivery/invoices") and m == "GET":
                _, deny = portal_access(event, conn, PERM_VIEW)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(
                    200,
                    search_ar_invoices(
                        doc_num=q.get("doc_num"),
                        series_name=q.get("series_name"),
                        doc_date=q.get("doc_date"),
                        card_code=q.get("card_code"),
                        card_name=q.get("card_name"),
                        doc_entry=q.get("doc_entry"),
                    ),
                )

            if p.startswith("/v1/delivery/invoices/") and m == "GET":
                _, deny = portal_access(event, conn, PERM_VIEW)
                if deny:
                    return json_response(403, {"error": deny})
                params = event.get("pathParameters") or {}
                seg = str(params.get("docEntry") or "").strip()
                if not seg:
                    seg = p.split("/v1/delivery/invoices/", 1)[-1].strip()
                de = int(seg)
                include_lines = q.get("include_lines") not in ("0", "false", "no")
                return json_response(200, get_ar_invoice_detail(de, include_lines=include_lines))

            if p.endswith("/v1/delivery/lr/verify") and m == "POST":
                _, deny = portal_access(event, conn, PERM_EDIT)
                if deny:
                    return json_response(403, {"error": deny})
                de_raw = b.get("doc_entry")
                de = int(de_raw) if de_raw not in (None, "") else None
                return json_response(
                    200,
                    verify_lr(
                        conn,
                        str(b.get("lrnum") or ""),
                        doc_entry=de,
                        doc_num=b.get("doc_num"),
                        series=b.get("series"),
                    ),
                )

            if p.endswith("/v1/delivery/lr/submit") and m == "POST":
                _, deny = portal_access(event, conn, PERM_EDIT)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(
                    200,
                    submit_lr_and_sync_sap(
                        conn,
                        doc_entry=int(b.get("doc_entry") or 0),
                        lrnum=str(b.get("lrnum") or ""),
                        doc_num=b.get("doc_num"),
                        series=b.get("series"),
                    ),
                )

            if p.endswith("/v1/delivery/lr/sap-state") and m == "GET":
                _, deny = portal_access(event, conn, PERM_VIEW)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(
                    200,
                    portal_lr_row_sap_state(
                        conn,
                        str(q.get("lrnum") or ""),
                        int(q.get("doc_entry") or 0),
                    ),
                )

            if p.endswith("/v1/delivery/tracker") and m == "GET":
                _, deny = portal_access(event, conn, PERM_VIEW)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(
                    200,
                    get_tracker_records(
                        conn,
                        lrnum=q.get("lrnum"),
                        doc_entry=q.get("doc_entry"),
                        doc_num=q.get("doc_num"),
                        status=q.get("status"),
                        page=int(q.get("page") or 1),
                        page_size=int(q.get("page_size") or 50),
                    ),
                )

            if p.startswith("/v1/delivery/tracker/") and m == "GET":
                params = event.get("pathParameters") or {}
                lrnum = str(params.get("lrnum") or "").strip()
                if not lrnum:
                    lrnum = p.split("/v1/delivery/tracker/", 1)[-1].strip()
                if lrnum:
                    _, deny = portal_access(event, conn, PERM_VIEW)
                    if deny:
                        return json_response(403, {"error": deny})
                    return json_response(200, get_tracker_lr_admin_detail(conn, lrnum))

            if p.endswith("/v1/delivery/alerts") and m == "GET":
                _, deny = portal_access(event, conn, PERM_VIEW)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(
                    200,
                    get_logistics_report_alerts(conn, int(q.get("limit") or 35)),
                )

            if p.endswith("/v1/delivery/sap-retry/count") and m == "GET":
                _, deny = portal_access(event, conn, PERM_VIEW)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(200, count_sap_retry_pending(conn))

            if p.endswith("/v1/delivery/sap-retry") and m == "POST":
                _, deny = portal_access(event, conn, PERM_EDIT)
                if deny:
                    return json_response(403, {"error": deny})
                if b.get("lrnum"):
                    result = retry_sap_sync_for_lr(conn, str(b.get("lrnum")))
                    if result.get("status") == "failed":
                        return json_response(400, result)
                    return json_response(200, result)
                return json_response(
                    200,
                    retry_sap_sync_all_failed(conn, int(b.get("max_rows") or 50)),
                )

            if p.endswith("/v1/delivery/pod/fix") and m == "POST":
                _, deny = portal_access(event, conn, PERM_EDIT)
                if deny:
                    return json_response(403, {"error": deny})
                return json_response(200, fix_pod_sap_from_rds(conn, str(b.get("lrnum") or "")))

        return json_response(404, {"error": "Not found"})
    except PortalValidationError as exc:
        return json_response(400, {"error": str(exc)})
    except ValueError as exc:
        return json_response(400, {"error": str(exc)})
    except RuntimeError as exc:
        # Common: missing hdbcli in layer, or HANA/SSM misconfigured.
        logger.exception("portal_handler runtime error")
        return json_response(503, {"error": str(exc)})
    except Exception as exc:
        logger.exception("portal_handler error")
        msg = str(exc).strip() or "Internal server error"
        # Surface a short cause so the dashboard toast is actionable.
        if "hdbcli" in msg.lower():
            msg = "HANA driver (hdbcli) missing from Lambda layer — rebuild shared layer on Linux/CI and redeploy delivery-api."
        elif "permission denied" in msg.lower() or "does not exist" in msg.lower():
            msg = f"Database error: {msg[:240]}"
        elif "timed out" in msg.lower() or "timeout" in msg.lower() or "can't connect" in msg.lower():
            msg = f"HANA connection failed: {msg[:240]}"
        else:
            msg = f"Internal server error: {msg[:240]}"
        return json_response(500, {"error": msg, "detail": traceback.format_exc()[-800:]})

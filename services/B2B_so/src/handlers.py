"""Lambda wiring: scheduled fetch + Cognito dashboard API."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

# Handler is src.handlers; local modules live next to this file.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import blinkit
import flipkart
import b2b_sappost
import cognito_admin
import permissions
import sap_hana_sync
import store

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

ROLE_ADMIN = "B2BAdmin"
ROLE_REVIEWER = "B2BReviewer"
ROLE_VIEWER = "B2BViewer"
_WRITE = {ROLE_ADMIN, ROLE_REVIEWER}

_DSN_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _scrub(text: str) -> str:
    return _DSN_RE.sub("://***:***@", text)


def _groups(event: dict[str, Any]) -> list[str]:
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    raw = claims.get("cognito:groups") or ""
    if isinstance(raw, list):
        return [str(g) for g in raw]
    text = str(raw).strip()
    if not text:
        return []
    return [p for p in text.replace("[", " ").replace("]", " ").replace(",", " ").split() if p]


def _actor(event: dict[str, Any]) -> str:
    claims = (((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt") or {}).get("claims") or {}
    return str(claims.get("email") or claims.get("cognito:username") or "").strip()


def _role(groups: list[str]) -> str:
    if ROLE_ADMIN in groups:
        return ROLE_ADMIN
    if ROLE_REVIEWER in groups:
        return ROLE_REVIEWER
    return ROLE_VIEWER


def _can_write(groups: list[str]) -> bool:
    return bool(set(groups) & _WRITE)


def _can_admin(groups: list[str]) -> bool:
    return ROLE_ADMIN in groups


def _run(label: str, fn):
    try:
        result = fn()
        logger.info("%s result: %s", label, result)
        return result
    except Exception as exc:
        blinkit.send_alert(f"{label} crashed", _scrub(f"{exc}\n\n{traceback.format_exc()[-3000:]}"))
        raise


def _lambda_client():
    import boto3

    return boto3.client("lambda")


def _invoke_fetch(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand off IMAP fetch to the background Lambda (httpApi caps sync calls at ~30s)."""
    name = (os.environ.get("B2B_FETCH_FUNCTION_NAME") or "").strip()
    if not name:
        raise RuntimeError("B2B_FETCH_FUNCTION_NAME is not configured")
    body = {"trigger": "manual", **payload}
    _lambda_client().invoke(
        FunctionName=name,
        InvocationType="Event",
        Payload=json.dumps(body, default=str),
    )
    return {"accepted": True, **body}


def _invoke_sap_post(payload: dict[str, Any]) -> dict[str, Any]:
    """Hand off SAP create/update/cancel to b2bSapPost (avoids API Gateway 30s cut-off)."""
    name = (os.environ.get("B2B_SAP_POST_FUNCTION_NAME") or "").strip()
    if not name:
        raise RuntimeError("B2B_SAP_POST_FUNCTION_NAME is not configured")
    body = {"trigger": "async", **payload}
    _lambda_client().invoke(
        FunctionName=name,
        InvocationType="Event",
        Payload=json.dumps(body, default=str),
    )
    return {
        "accepted": True,
        "status": "posting",
        "message": "Posting to SAP — this can take up to a few minutes. Keep this page open.",
        "po_number": body.get("po_number"),
        "channel": body.get("channel"),
        "action": body.get("action"),
    }


def _mark_sap_posting(conn, po: str, channel: str, pipeline_status: str) -> None:
    store.set_po_status(
        conn,
        po,
        pipeline_status,
        channel=channel,
        sap_status="POSTING",
        last_error="",
        sap_response={"status": "posting", "message": "SAP post in progress"},
    )
    conn.commit()


def _clear_stuck_posting(conn, po: str, channel: str, message: str) -> None:
    detail = store.get_po(conn, po, channel=channel)
    if not detail:
        return
    header = detail["header"]
    if str(header.get("sap_status") or "").upper() != "POSTING":
        return
    pipe = str(header.get("pipeline_status") or store.STATUS_READY)
    sap_status = "POST FAILED" if pipe != store.STATUS_POSTED else "SENT TO SAP"
    store.set_po_status(
        conn,
        po,
        pipe,
        channel=channel,
        sap_status=sap_status,
        last_error=message,
        sap_response={"status": "error", "message": message},
    )
    conn.commit()


def fetch_handler(event, context):
    ev = event or {}
    channel = str(ev.get("channel") or store.CHANNEL_BLINKIT).strip().lower()
    if channel == flipkart.CHANNEL:
        return _run("flipkart_fetch", lambda: flipkart.run_fetch(max_messages=ev.get("max_messages")))
    return _run(
        f"{channel}_fetch",
        lambda: blinkit.run_fetch(channel=channel, max_messages=ev.get("max_messages")),
    )


def sap_post_handler(event, context):
    """Background SAP Operations (approve/create, update, cancel)."""
    ev = event or {}
    action = str(ev.get("action") or "approve").strip().lower()
    po = str(ev.get("po_number") or "").strip()
    ch = str(ev.get("channel") or store.CHANNEL_BLINKIT).strip().lower()
    actor = str(ev.get("actor") or "").strip()
    code = str(ev.get("salesperson_code") or "").strip()

    def work():
        with store.connect() as conn:
            try:
                if action in ("approve", "post"):
                    result = b2b_sappost.post_purchase_order(
                        conn, po, actor=actor, salesperson_code=code, channel=ch
                    )
                    if result.get("status") == "success":
                        store.add_audit(
                            conn,
                            actor=actor,
                            action="approve_post",
                            po_number=po,
                            channel=ch,
                            detail=str(result.get("docentry") or ""),
                        )
                        conn.commit()
                    else:
                        _clear_stuck_posting(
                            conn, po, ch, str(result.get("message") or result.get("last_error") or "SAP post failed")
                        )
                    return result
                if action == "update":
                    result = b2b_sappost.update_posted_order(
                        conn, po, actor=actor, salesperson_code=code, channel=ch
                    )
                    if result.get("status") == "success":
                        try:
                            sap_hana_sync.sync_po_from_hana(conn, po, channel=ch)
                            conn.commit()
                        except Exception:
                            logger.warning("HANA sync after SAP update skipped for %s", po, exc_info=True)
                    else:
                        _clear_stuck_posting(
                            conn, po, ch, str(result.get("message") or result.get("last_error") or "SAP update failed")
                        )
                    return result
                if action == "cancel":
                    result = b2b_sappost.cancel_posted_order(conn, po, actor=actor, channel=ch)
                    if result.get("status") != "success":
                        _clear_stuck_posting(
                            conn, po, ch, str(result.get("message") or result.get("last_error") or "SAP cancel failed")
                        )
                    return result
                raise ValueError(f"Unknown SAP action: {action}")
            except Exception as exc:
                msg = _scrub(str(exc))
                detail = store.get_po(conn, po, channel=ch)
                if detail and action in ("approve", "post"):
                    b2b_sappost._record_post_failure(conn, po, detail["header"], msg, actor, channel=ch)
                else:
                    _clear_stuck_posting(conn, po, ch, msg)
                raise

    return _run(f"sap_{action}_{ch}_{po}", work)


def _json(status: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _method(event: dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _query(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    return {str(k): str(v) for k, v in params.items() if v is not None} if isinstance(params, dict) else {}


def _body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _profile(conn, actor: str, groups: list[str]) -> dict[str, Any]:
    user = store.get_user_by_email(conn, actor) if actor else None
    if user and user.get("is_active"):
        return user
    fallback = {
        "email": actor,
        "full_name": "",
        "role": "viewer",
        "sap_salesperson_code": "",
        "is_active": False,
        "can_approve": False,
        "can_post_sap": False,
        "can_manage_users": _can_admin(groups),
        "channels": ["blinkit"] if not _can_admin(groups) else list(store.ALL_CHANNELS),
        "perm_orders": "",
        "perm_sku_map": "",
        "perm_ship_to": "",
        "perm_delivery": "",
    }
    return permissions.attach_permissions(fallback)


def _deny_perm(profile: dict[str, Any], resource: str, need: str) -> dict[str, Any] | None:
    if not permissions.has_perm(profile, resource, need):
        label = {"orders": "purchase orders", "sku_map": "item map", "ship_to": "ship-to"}.get(resource, resource)
        need_label = {"view": "view", "edit": "edit", "delete": "delete"}.get(need, need)
        return _json(403, {"error": f"Not allowed — need {need_label} access for {label}"})
    return None


def dashboard_handler(event, context):
    method = _method(event)
    if method == "OPTIONS":
        return _json(200, {"ok": True})
    path = _path(event)
    q = _query(event)
    groups = _groups(event)
    actor = _actor(event)

    try:
        with store.connect() as conn:
            profile = _profile(conn, actor, groups)

            if path.endswith("/v1/b2b/me") and method == "GET":
                try:
                    fetch_channels = blinkit.list_fetch_channels()
                except Exception:
                    logger.warning("Could not load fetch channels from IMAP config", exc_info=True)
                    fetch_channels = [store.CHANNEL_BLINKIT]
                return _json(200, {
                    **profile,
                    "groups": groups,
                    "card_code": blinkit.BLINKIT_CARD_CODE,
                    "all_channels": store.ALL_CHANNELS,
                    "fetch_channels": fetch_channels,
                })

            if path.endswith("/v1/b2b/fetch") and method == "POST":
                body = _body(event)
                channel = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT).strip().lower()
                if channel not in store.FETCH_CHANNELS:
                    return _json(400, {"error": f"Unknown fetch channel: {channel}"})
                payload: dict[str, Any] = {"trigger": "manual", "channel": channel}
                if body.get("max_messages") is not None:
                    payload["max_messages"] = body.get("max_messages")
                return _json(202, _invoke_fetch(payload))

            if path.endswith("/v1/b2b/stats") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                ch = q.get("channel") or store.CHANNEL_BLINKIT
                try:
                    sap_hana_sync.sync_posted_orders(conn, channel=ch)
                except Exception:
                    logger.warning("SAP HANA bulk sync skipped", exc_info=True)
                store.mark_notifications_read_for_posted_pos(conn, channel=ch)
                conn.commit()
                return _json(200, store.fetch_stats(conn, channel=ch))
            if path.endswith("/v1/b2b/orders") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                ch = q.get("channel") or store.CHANNEL_BLINKIT
                try:
                    sap_hana_sync.sync_posted_orders(conn, channel=ch)
                    conn.commit()
                except Exception:
                    logger.warning("SAP HANA bulk sync skipped on orders list", exc_info=True)
                return _json(
                    200,
                    store.list_pos(
                        conn,
                        channel=ch,
                        pipeline_status=q.get("status") or None,
                        po_number=(q.get("po_number") or "").strip() or None,
                        sap_docnum=(q.get("sap_docnum") or "").strip() or None,
                        sap_docentry=(q.get("sap_docentry") or "").strip() or None,
                        created_from=(q.get("created_from") or "").strip() or None,
                        created_to=(q.get("created_to") or "").strip() or None,
                        sort_by=(q.get("sort_by") or "created_at").strip(),
                        sort_dir=(q.get("sort_dir") or "desc").strip(),
                        limit=min(int(q.get("limit") or 50), 200),
                        offset=max(int(q.get("offset") or 0), 0),
                    ),
                )
            if path.endswith("/v1/b2b/order-detail") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                po = (q.get("po_number") or "").strip()
                ch = q.get("channel") or store.CHANNEL_BLINKIT
                detail = store.get_po(conn, po, channel=ch)
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                header = detail["header"]
                if header.get("sap_docentry") and header.get("pipeline_status") in {
                    store.STATUS_POSTED,
                    store.STATUS_CANCELLED,
                }:
                    try:
                        sap_hana_sync.sync_po_from_hana(conn, po, channel=ch)
                        detail = store.get_po(conn, po, channel=ch) or detail
                        header = detail["header"]
                    except Exception:
                        logger.warning("SAP HANA sync failed for PO %s", po, exc_info=True)
                header = dict(header)
                header["pdf_url"] = blinkit.public_url(header.get("pdf_s3_key") or "")
                header["xlsx_url"] = blinkit.public_url(header.get("xlsx_s3_key") or "")
                header["po_url"] = b2b_sappost.resolve_po_document_url(header)
                sap_live = None
                if header.get("sap_docentry"):
                    try:
                        sap_live = sap_hana_sync.get_sales_orders([int(header["sap_docentry"])]).get(
                            int(header["sap_docentry"])
                        )
                    except Exception:
                        logger.warning("Could not load live SAP row for PO %s", po, exc_info=True)
                if sap_live:
                    # Prefer live HANA status/total even if DB row is stale.
                    header["sap_document_status"] = str(
                        sap_live.get("DocumentStatus") or header.get("sap_document_status") or ""
                    )
                    if sap_live.get("DocTotal") is not None:
                        header["sap_doc_total"] = float(sap_live.get("DocTotal") or 0)
                    if sap_live.get("DocDate"):
                        header["sap_doc_date"] = str(sap_live.get("DocDate") or "")
                    if sap_live.get("DocNum") is not None:
                        header["sap_docnum"] = sap_live.get("DocNum")
                post_resp = header.get("sap_response")
                amend_history = store.list_po_amend_history(conn, po, channel=ch, include_record_data=True)
                activity_log = store.list_po_audit(conn, po, channel=ch)
                return _json(
                    200,
                    {
                        "header": header,
                        "lines": detail["lines"],
                        "sap_live": sap_live,
                        "sap_post_response": post_resp,
                        "amend_history": amend_history,
                        "activity_log": activity_log,
                    },
                )
            if path.endswith("/v1/b2b/amend-history") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                po = (q.get("po_number") or "").strip()
                ch = q.get("channel") or store.CHANNEL_BLINKIT
                amend_id = (q.get("amend_id") or "").strip()
                if amend_id:
                    try:
                        row = store.get_po_amend_by_id(conn, int(amend_id), channel=ch)
                    except ValueError:
                        return _json(400, {"error": "Invalid amend_id"})
                    if not row or (po and row.get("po_number") != po):
                        return _json(404, {"error": "Amend snapshot not found"})
                    return _json(200, {"item": row})
                if not po:
                    return _json(400, {"error": "po_number is required"})
                items = store.list_po_amend_history(conn, po, channel=ch, include_record_data=True)
                return _json(200, {"po_number": po, "items": items})
            if path.endswith("/v1/b2b/order-rematch") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                po = (_body(event).get("po_number") or q.get("po_number") or "").strip()
                ch = str(_body(event).get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                try:
                    if ch == flipkart.CHANNEL:
                        result = flipkart.rematch_po(conn, po)
                    else:
                        result = blinkit.rematch_po(conn, po, channel=ch)
                except ValueError as exc:
                    return _json(400, {"error": str(exc)})
                store.add_audit(conn, actor=actor, action="rematch_po", po_number=po, channel=ch, detail=result.get("last_error") or "ok")
                conn.commit()
                return _json(200, result)
            if path.endswith("/v1/b2b/approve") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                if not profile.get("can_post_sap"):
                    return _json(403, {"error": "Your role cannot post to SAP"})
                po = (_body(event).get("po_number") or q.get("po_number") or "").strip()
                ch = str(_body(event).get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                code = (profile.get("sap_salesperson_code") or "").strip()
                if not code:
                    return _json(400, {"error": "Your user has no SAP salesperson code. Ask admin to set it."})
                detail = store.get_po(conn, po, channel=ch)
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                header = detail["header"]
                postable = {store.STATUS_READY, store.STATUS_APPROVED}
                if header.get("pipeline_status") not in postable:
                    return _json(400, {
                        "error": f"PO is not ready for posting (status={header.get('pipeline_status')}). Click Refetch first.",
                    })
                # Block validation errors; allow retry after a prior SAP post failure / in-flight POSTING.
                if (header.get("last_error") or "").strip():
                    sap_st = str(header.get("sap_status") or "").upper()
                    if "FAIL" not in sap_st and sap_st != "POSTING":
                        return _json(400, {
                            "error": "Still blocked. Update maps, then click Refetch. " + header["last_error"],
                            "last_error": header["last_error"],
                        })
                tax_gaps = blinkit.tax_code_gap_sap_codes(detail["lines"], header=header)
                if tax_gaps:
                    return _json(400, {
                        "error": "Missing tax code on PO lines for: "
                        + ", ".join(tax_gaps)
                        + ". Click Refetch first.",
                        "last_error": header.get("last_error") or "",
                    })
                if link_err := b2b_sappost.po_link_error(header):
                    return _json(400, {
                        "error": link_err,
                        "last_error": header.get("last_error") or "",
                    })
                _mark_sap_posting(conn, po, ch, str(header.get("pipeline_status") or store.STATUS_READY))
                try:
                    return _json(
                        202,
                        _invoke_sap_post(
                            {
                                "action": "approve",
                                "po_number": po,
                                "channel": ch,
                                "actor": actor,
                                "salesperson_code": code,
                            }
                        ),
                    )
                except Exception as exc:
                    _clear_stuck_posting(conn, po, ch, _scrub(str(exc)))
                    raise
            if path.endswith("/v1/b2b/reject") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                body = _body(event)
                po = (body.get("po_number") or q.get("po_number") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                reason = str(body.get("reason") or "Rejected by reviewer").strip()
                detail = store.get_po(conn, po, channel=ch)
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                if detail["header"].get("pipeline_status") == store.STATUS_POSTED:
                    return _json(400, {"error": "Posted POs cannot be rejected"})
                store.set_po_status(conn, po, store.STATUS_REJECTED, last_error=reason, channel=ch)
                store.mark_notifications_read(conn, po_number=po, channel=ch)
                store.add_audit(conn, actor=actor, action="reject", po_number=po, channel=ch, detail=reason[:500])
                conn.commit()
                return _json(200, {"status": "rejected", "po_number": po, "message": "PO rejected — not sent to SAP"})
            if path.endswith("/v1/b2b/post-sap") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                if not profile.get("can_post_sap"):
                    return _json(403, {"error": "Not allowed for your role"})
                code = (profile.get("sap_salesperson_code") or "").strip()
                po = (_body(event).get("po_number") or q.get("po_number") or "").strip()
                ch = str(_body(event).get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                if not code:
                    msg = "Your user has no SAP salesperson code. Ask admin to set it."
                    detail = store.get_po(conn, po, channel=ch) if po else None
                    if detail:
                        b2b_sappost._record_post_failure(conn, po, detail["header"], msg, actor, channel=ch)
                    return _json(200, {"status": "error", "message": msg, "last_error": msg})
                detail = store.get_po(conn, po, channel=ch) if po else None
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                _mark_sap_posting(
                    conn, po, ch, str(detail["header"].get("pipeline_status") or store.STATUS_READY)
                )
                try:
                    return _json(
                        202,
                        _invoke_sap_post(
                            {
                                "action": "post",
                                "po_number": po,
                                "channel": ch,
                                "actor": actor,
                                "salesperson_code": code,
                            }
                        ),
                    )
                except Exception as exc:
                    _clear_stuck_posting(conn, po, ch, _scrub(str(exc)))
                    raise
            if path.endswith("/v1/b2b/update-sap") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                if not profile.get("can_post_sap"):
                    return _json(403, {"error": "Not allowed for your role"})
                body = _body(event)
                po = (body.get("po_number") or q.get("po_number") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                code = (profile.get("sap_salesperson_code") or "").strip()
                if not code:
                    return _json(400, {"error": "Your user has no SAP salesperson code. Ask admin to set it."})
                detail = store.get_po(conn, po, channel=ch)
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                _mark_sap_posting(
                    conn, po, ch, str(detail["header"].get("pipeline_status") or store.STATUS_POSTED)
                )
                try:
                    return _json(
                        202,
                        _invoke_sap_post(
                            {
                                "action": "update",
                                "po_number": po,
                                "channel": ch,
                                "actor": actor,
                                "salesperson_code": code,
                            }
                        ),
                    )
                except Exception as exc:
                    _clear_stuck_posting(conn, po, ch, _scrub(str(exc)))
                    raise
            if path.endswith("/v1/b2b/cancel-sap") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                if not profile.get("can_post_sap"):
                    return _json(403, {"error": "Not allowed for your role"})
                body = _body(event)
                po = (body.get("po_number") or q.get("po_number") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                detail = store.get_po(conn, po, channel=ch)
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                _mark_sap_posting(
                    conn, po, ch, str(detail["header"].get("pipeline_status") or store.STATUS_POSTED)
                )
                try:
                    return _json(
                        202,
                        _invoke_sap_post(
                            {
                                "action": "cancel",
                                "po_number": po,
                                "channel": ch,
                                "actor": actor,
                                "salesperson_code": (profile.get("sap_salesperson_code") or "").strip(),
                            }
                        ),
                    )
                except Exception as exc:
                    _clear_stuck_posting(conn, po, ch, _scrub(str(exc)))
                    raise
            if path.endswith("/v1/b2b/users") and method == "GET":
                if not profile.get("can_manage_users"):
                    return _json(403, {"error": "Not allowed for your role"})
                return _json(200, {"items": store.list_users(conn)})
            if path.endswith("/v1/b2b/users") and method == "PUT":
                if not profile.get("can_manage_users"):
                    return _json(403, {"error": "Not allowed for your role"})
                body = _body(event)
                email = str(body.get("email") or "").strip()
                password = str(body.get("password") or "")
                role = str(body.get("role") or "viewer")
                if password:
                    try:
                        cognito_admin.ensure_login(email=email, password=password, role=role)
                    except ValueError as exc:
                        return _json(400, {"error": str(exc)})
                elif not cognito_admin.has_login(email):
                    return _json(400, {"error": "Password is required so this person can sign in"})
                raw_channels = body.get("channels")
                store.upsert_user(
                    conn,
                    email=email,
                    full_name=str(body.get("full_name") or ""),
                    role=role,
                    sap_salesperson_code=str(body.get("sap_salesperson_code") or ""),
                    is_active=bool(body.get("is_active", True)),
                    channels=list(raw_channels) if isinstance(raw_channels, list) else None,
                    perm_orders=str(body.get("perm_orders") or ""),
                    perm_sku_map=str(body.get("perm_sku_map") or ""),
                    perm_ship_to=str(body.get("perm_ship_to") or ""),
                    perm_delivery=str(body.get("perm_delivery") or ""),
                )
                store.add_audit(conn, actor=actor, action="upsert_user", detail=email)
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/users/status") and method == "POST":
                if not profile.get("can_manage_users"):
                    return _json(403, {"error": "Not allowed for your role"})
                body = _body(event)
                email = str(body.get("email") or "").strip()
                is_active = bool(body.get("is_active"))
                if email.lower() == actor.lower() and not is_active:
                    return _json(400, {"error": "You cannot deactivate your own login"})
                if not store.set_user_active(conn, email, is_active):
                    return _json(404, {"error": "User not found"})
                try:
                    cognito_admin.set_login_enabled(email, is_active)
                except ValueError as exc:
                    return _json(400, {"error": str(exc)})
                store.add_audit(conn, actor=actor, action="set_user_active", detail=f"{email} active={is_active}")
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/users") and method == "DELETE":
                if not profile.get("can_manage_users"):
                    return _json(403, {"error": "Not allowed for your role"})
                email = str((_body(event).get("email") or q.get("email") or "")).strip()
                if not email:
                    return _json(400, {"error": "email is required"})
                if email.lower() == actor.lower():
                    return _json(400, {"error": "You cannot delete your own login"})
                store.delete_user(conn, email)
                try:
                    cognito_admin.delete_login(email)
                except ValueError as exc:
                    return _json(400, {"error": str(exc)})
                store.add_audit(conn, actor=actor, action="delete_user", detail=email)
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/logs") and method == "GET":
                if not profile.get("can_manage_users"):
                    return _json(403, {"error": "Not allowed for your role"})
                return _json(200, {"items": store.list_audit(conn, channel=q.get("channel") or None)})
            if path.endswith("/v1/b2b/notifications") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                unread = (q.get("unread") or "").lower() in {"1", "true"}
                allowed = {
                    str(c).strip().lower()
                    for c in (profile.get("channels") or [])
                    if str(c).strip() and str(c).strip().lower() != store.CHANNEL_LOGISTICS
                }
                raw_multi = str(q.get("channels") or "").strip()
                if raw_multi:
                    requested = [p.strip().lower() for p in raw_multi.split(",") if p.strip()]
                    chans = [c for c in requested if c in allowed]
                else:
                    ch = str(q.get("channel") or store.CHANNEL_BLINKIT).strip().lower()
                    chans = [ch] if ch in allowed else []
                if not chans:
                    return _json(200, {"items": []})
                for ch in chans:
                    store.mark_notifications_read_for_posted_pos(conn, channel=ch)
                conn.commit()
                return _json(
                    200,
                    {
                        "items": store.list_notifications(
                            conn, unread_only=unread, channels=chans, limit=80
                        )
                    },
                )
            if path.endswith("/v1/b2b/notifications/read") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                body = _body(event)
                allowed = {
                    str(c).strip().lower()
                    for c in (profile.get("channels") or [])
                    if str(c).strip() and str(c).strip().lower() != store.CHANNEL_LOGISTICS
                }
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT).strip().lower()
                if ch not in allowed:
                    return _json(403, {"error": "Not allowed for this channel"})
                nid = body.get("id")
                po = str(body.get("po_number") or "").strip()
                if po:
                    store.mark_notifications_read(conn, po_number=po, channel=ch)
                else:
                    store.mark_notifications_read(conn, notification_id=int(nid) if nid else None, channel=ch)
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/notifications") and method == "DELETE":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                body = _body(event)
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                nid = body.get("id")
                if not nid:
                    return _json(400, {"error": "id is required"})
                if not store.delete_notification(conn, int(nid), channel=ch):
                    return _json(404, {"error": "Notification not found"})
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/sku-map/import") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_SKU_MAP, permissions.PERM_EDIT):
                    return deny
                body = _body(event)
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                count = store.import_sku_map(conn, body.get("items") or [], channel=ch)
                store.add_audit(conn, actor=actor, action="sku_map_import", detail=str(count), channel=ch)
                conn.commit()
                return _json(200, {"ok": True, "count": count})
            if path.endswith("/v1/b2b/sku-map/preview") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_SKU_MAP, permissions.PERM_VIEW):
                    return deny
                body = _body(event)
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                item_code = str(body.get("item_code") or "").strip()
                mapped = store.sku_map_dict(conn, channel=ch)
                sap_code = mapped.get(item_code) or ""
                return _json(200, {"ok": bool(sap_code), "item_code": item_code, "sap_code": sap_code})
            if path.endswith("/v1/b2b/sku-map") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_SKU_MAP, permissions.PERM_VIEW):
                    return deny
                return _json(200, {"items": store.list_sku_map(conn, channel=q.get("channel") or store.CHANNEL_BLINKIT)})
            if path.endswith("/v1/b2b/order") and method == "PUT":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_EDIT):
                    return deny
                body = _body(event)
                po = str(body.get("po_number") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                try:
                    updated = store.update_po(conn, po, body.get("header") or {}, body.get("lines") or [], channel=ch)
                except ValueError as exc:
                    return _json(400, {"error": str(exc)})
                store.add_audit(conn, actor=actor, action="edit_po", po_number=po, channel=ch)
                conn.commit()
                return _json(200, updated)
            if path.endswith("/v1/b2b/sku-map") and method == "PUT":
                if deny := _deny_perm(profile, permissions.RESOURCE_SKU_MAP, permissions.PERM_EDIT):
                    return deny
                item_code = str(_body(event).get("item_code") or "").strip()
                sap_code = str(_body(event).get("sap_code") or "").strip()
                ch = str(_body(event).get("channel") or store.CHANNEL_BLINKIT)
                store.upsert_sku(conn, item_code, sap_code, channel=ch)
                store.add_audit(conn, actor=actor, action="sku_map", detail=f"{item_code}->{sap_code}", channel=ch)
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/sku-map") and method == "DELETE":
                if deny := _deny_perm(profile, permissions.RESOURCE_SKU_MAP, permissions.PERM_DELETE):
                    return deny
                body = _body(event)
                item_code = str(body.get("item_code") or q.get("item_code") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                if not item_code:
                    return _json(400, {"error": "item_code is required"})
                store.delete_sku(conn, item_code, channel=ch)
                store.add_audit(conn, actor=actor, action="sku_map_delete", detail=item_code, channel=ch)
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/ship-to/import") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_SHIP_TO, permissions.PERM_EDIT):
                    return deny
                body = _body(event)
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                count = store.import_ship_to_aliases(conn, body.get("items") or [], channel=ch)
                store.add_audit(conn, actor=actor, action="ship_to_import", detail=str(count), channel=ch)
                conn.commit()
                return _json(200, {"ok": True, "count": count})
            if path.endswith("/v1/b2b/ship-to/preview") and method == "POST":
                if deny := _deny_perm(profile, permissions.RESOURCE_SHIP_TO, permissions.PERM_VIEW):
                    return deny
                body = _body(event)
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                text = str(body.get("address_entry") or body.get("delivered_to") or "").strip()
                aliases = store.list_ship_to_aliases(conn, channel=ch)
                return _json(200, blinkit.match_ship_to(text, aliases))
            if path.endswith("/v1/b2b/ship-to") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_SHIP_TO, permissions.PERM_VIEW):
                    return deny
                return _json(200, {"items": store.list_ship_to_aliases(conn, channel=q.get("channel") or store.CHANNEL_BLINKIT)})
            if path.endswith("/v1/b2b/ship-to") and method == "PUT":
                if deny := _deny_perm(profile, permissions.RESOURCE_SHIP_TO, permissions.PERM_EDIT):
                    return deny
                body = _body(event)
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                if body.get("address_id"):
                    store.upsert_ship_to_alias(conn, str(body["address_id"]), str(body.get("address_entry") or ""), channel=ch)
                    store.add_audit(conn, actor=actor, action="ship_to_upsert", detail=str(body.get("address_id")), channel=ch)
                    conn.commit()
                    return _json(200, {"ok": True})
                rows = body.get("items") or []
                store.replace_ship_to_aliases(conn, rows, channel=ch)
                store.add_audit(conn, actor=actor, action="ship_to_replace", detail=str(len(rows)), channel=ch)
                conn.commit()
                return _json(200, {"ok": True, "count": len(rows)})
            if path.endswith("/v1/b2b/ship-to") and method == "DELETE":
                if deny := _deny_perm(profile, permissions.RESOURCE_SHIP_TO, permissions.PERM_DELETE):
                    return deny
                body = _body(event)
                address_id = str(body.get("address_id") or q.get("address_id") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                if not address_id:
                    return _json(400, {"error": "address_id is required"})
                store.delete_ship_to_alias(conn, address_id, channel=ch)
                store.add_audit(conn, actor=actor, action="ship_to_delete", detail=address_id, channel=ch)
                conn.commit()
                return _json(200, {"ok": True})
            if path.endswith("/v1/b2b/order-preview") and method == "GET":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_VIEW):
                    return deny
                po = (q.get("po_number") or "").strip()
                ch = q.get("channel") or store.CHANNEL_BLINKIT
                detail = store.get_po(conn, po, channel=ch)
                if not detail:
                    return _json(404, {"error": "PO not found", "po_number": po})
                header = dict(detail["header"])
                header["channel"] = str(ch).strip().lower()
                lines = list(detail["lines"])
                header["pdf_url"] = blinkit.public_url(header.get("pdf_s3_key") or header.get("pdf_s3_key") or "")
                header["xlsx_url"] = blinkit.public_url(header.get("xlsx_s3_key") or header.get("xlsx_s3_key") or "")
                header["po_url"] = b2b_sappost.resolve_po_document_url(header)
                code = str(profile.get("sap_salesperson_code") or "")
                payload = b2b_sappost.build_order_payload(header, lines, code, skip_enrich=True)
                extract = {"header": header, "lines": lines}
                return _json(200, {"header": header, "lines": lines, "payload": payload, "extract": extract})
            if path.endswith("/v1/b2b/order") and method == "DELETE":
                if deny := _deny_perm(profile, permissions.RESOURCE_ORDERS, permissions.PERM_DELETE):
                    return deny
                body = _body(event)
                po = str(body.get("po_number") or q.get("po_number") or "").strip()
                ch = str(body.get("channel") or q.get("channel") or store.CHANNEL_BLINKIT)
                if not store.delete_po(conn, po, channel=ch):
                    return _json(400, {"error": "PO was not deleted. Posted POs cannot be removed."})
                store.add_audit(conn, actor=actor, action="delete_po", po_number=po, channel=ch)
                conn.commit()
                return _json(200, {"ok": True})
        return _json(404, {"error": "Not found", "path": path})
    except Exception as exc:
        logger.error("dashboard_api_error path=%s error=%s", path, _scrub(str(exc)))
        return _json(500, {"error": _scrub(str(exc))})

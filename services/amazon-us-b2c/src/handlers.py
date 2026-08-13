"""Lambda entrypoints. Thin: parse event -> open DB -> run worker -> report.

Every handler alerts via SES on an unhandled crash and re-raises so the
invocation is marked failed in CloudWatch (EventBridge metrics stay
honest).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Lambda package root is ``src/``; local modules use ``from amazon_us.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from amazon_us import constants, dashboard_queries, fetch_sync, post_credit_notes, post_invoices, recon, store
from amazon_us.alerts import send_alert
from amazon_us.fba_ship_sync import sync_ship_dates

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# DATABASE_URL embeds a password (postgresql://user:pass@host/...); some
# driver errors echo the DSN back in the exception text, so scrub it before
# any crash detail goes out in an alert email.
_DSN_CREDS_RE = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _scrub(text: str) -> str:
    return _DSN_CREDS_RE.sub("://***:***@", text)


def _run(label: str, fn):
    try:
        result = fn()
        logger.info("%s result: %s", label, result)
        return result
    except Exception as e:
        send_alert(f"{label} crashed", _scrub(f"{e}\n\n{traceback.format_exc()[-3000:]}"))
        raise


def fetch_handler(event, context):
    """modes: incremental (default) | report | backfill | seed_watermark.
    report/backfill events: {"mode": "report", "from_date": "YYYY-MM-DD",
    "to_date": "YYYY-MM-DD", "lookback_days": 30?}
    seed_watermark event: {"mode": "seed_watermark", "when": "YYYY-MM-DD HH:MM:SS"?}
    -- sets the incremental watermark directly, defaulting to now. Run once
    before ever enabling the incremental schedule (a missing watermark makes
    sync_incremental try a 30-day per-order catch-up that can exhaust the
    900s Lambda timeout without finishing)."""
    mode = (event or {}).get("mode", "incremental")

    def _go():
        with store.connect() as conn:
            if mode == "incremental":
                return fetch_sync.sync_incremental(conn)
            if mode in ("report", "backfill"):
                from_date = event.get("from_date") or date.today().replace(day=1).isoformat()
                to_date = event.get("to_date") or date.today().isoformat()
                return fetch_sync.sync_via_report(conn, from_date, to_date, event.get("lookback_days"))
            if mode == "seed_watermark":
                value = fetch_sync.seed_watermark(conn, event.get("when"))
                return {"watermark_set_to": value}
            raise ValueError(f"Unknown fetch mode: {mode}")

    return _run(f"fetch[{mode}]", _go)


def fba_ship_sync_handler(event, context):
    """Default window: last 21 days (Pacific). Manual: {"from_date", "to_date"}.
    debug_resume_report: {"mode": "debug_resume_report", "report_id": str} — Amazon's
    report queue can take much longer than one Lambda invoke's poll budget on a large
    window; this resumes polling an ALREADY-REQUESTED report_id (from a previous
    timeout's logs) instead of creating a brand new report request each retry."""

    def _go():
        ev = event or {}
        if ev.get("mode") == "debug_resume_report":
            from amazon_us import reports_api
            from amazon_us.fba_ship_sync import parse_earliest_ship_dates

            report_id = ev["report_id"]
            doc_id = reports_api.poll_report(report_id)
            content = reports_api.download_report(doc_id)
            ship_dates = parse_earliest_ship_dates(content)
            with store.connect() as conn:
                updated = store.set_actual_ship_dates(conn, ship_dates)
            return {"status": "success", "parsed": len(ship_dates), "updated": updated}
        if ev.get("mode") == "debug_cancel_report":
            from amazon_us import reports_api

            return {"cancelled": reports_api.cancel_report(ev["report_id"])}
        if ev.get("mode") == "debug_rate_limit_check":
            # ONE raw request, zero retries -- sp_api.post() retries up to 20x
            # internally with backoff, so even a "quick" create_report call
            # blocks for minutes while still rate-limited. This bypasses that
            # to give an instant yes/no on whether the createReport quota has
            # recovered, before committing to a real (possibly 900s) attempt.
            import requests as _requests

            from amazon_us import constants as _constants
            from amazon_us import sp_api as _sp_api

            url = _constants.SP_API_BASE + "/reports/2021-06-30/reports"
            body = {
                "reportType": "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL",
                "marketplaceIds": [_constants.MARKETPLACE_ID],
                "dataStartTime": "2026-08-10T00:00:00Z",
                "dataEndTime": "2026-08-11T00:00:00Z",
            }
            resp = _requests.post(url, headers=_sp_api._headers(), json=body, timeout=15)
            return {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp.text[:1000],
            }

        from_date = ev.get("from_date") or (date.today() - timedelta(days=21)).isoformat()
        to_date = ev.get("to_date") or date.today().isoformat()
        with store.connect() as conn:
            return sync_ship_dates(conn, from_date, to_date)

    return _run("fba_ship_sync", _go)


def post_invoices_handler(event, context):
    """{"limit": 200?, "dry_run": false?, "mode": "post"|"retry"|"list_ship_to_codes"}
    {"amazon_order_id": str, "dry_run": false?} — manual pilot helper,
    posts exactly that one order (bypasses ship-date ordering / limit).
    list_ship_to_codes: read-only SAP query, no DB, no posting — returns
    the exact AddressName values SAP currently has for this BP's Ship-To
    addresses (the valid ShipToCode/PayToCode set)."""

    def _go():
        ev = event or {}
        if ev.get("mode") == "list_ship_to_codes":
            from amazon_us import sap_client
            return {"card_code": constants.SAP_CARD_CODE, "ship_to_codes": sap_client.list_ship_to_addresses(constants.SAP_CARD_CODE)}
        if ev.get("mode") == "debug_invoice_doc":
            from amazon_us import sap_client
            return sap_client.get_invoice_doc_debug(int(ev["doc_entry"]))
        if ev.get("mode") == "debug_invoice_lines":
            from amazon_us import sap_client
            doc = sap_client.get_invoice_doc(int(ev["doc_entry"]))
            return {"DocTotal": doc.get("DocTotal"), "DocumentLines": doc.get("DocumentLines")}
        if ev.get("mode") == "debug_find_all_invoices":
            from amazon_us import sap_client
            return {"invoices": sap_client.find_all_invoices_by_order_debug(ev["amazon_order_id"])}
        if ev.get("mode") == "debug_sku_check":
            from amazon_us.hana_access import fetch_amazon_sku_map
            raw = fetch_amazon_sku_map()
            targets = ev.get("skus") or []
            return {sku: raw.get(sku, "NOT FOUND") for sku in targets}
        if ev.get("mode") == "debug_build_payload":
            from amazon_us.hana_access import allocate_batches_fefo, get_bom_children, resolve_acct_code
            from amazon_us.invoice_payload import build_invoice_payload
            with store.connect() as conn:
                mapping = store.get_sku_mapping(conn)
                order = store.get_order(conn, ev["amazon_order_id"])
            payload, skipped = build_invoice_payload(order, mapping, allocate_batches_fefo, resolve_acct_code, get_bom_children)
            return {"payload": payload, "skipped": skipped}
        if ev.get("mode") == "export_recon":
            return _export_recon(ev["period_from"], ev["period_to"], ev.get("s3_bucket") or constants.RECON_BUCKET)
        if ev.get("mode") == "export_finance_revenue_report":
            return _export_finance_revenue_report(ev["period_from"], ev["period_to"], ev.get("s3_bucket") or constants.RECON_BUCKET)
        if ev.get("mode") == "debug_bom_check":
            from khanal_shared.hana.pool import hana_connection
            out = {}
            with hana_connection() as conn:
                cur = conn.cursor()
                for item_code in ev.get("item_codes") or []:
                    cur.execute(
                        'SELECT "ItemCode", "ItemName", "TreeType", "InvntItem" FROM "OITM" WHERE "ItemCode" = ?',
                        [item_code],
                    )
                    itm = cur.fetchone()
                    cur.execute(
                        'SELECT "Code", "Quantity", "Warehouse" FROM "ITT1" WHERE "Father" = ?',
                        [item_code],
                    )
                    children = cur.fetchall() or []
                    out[item_code] = {
                        "item_master": list(itm) if itm else "NOT FOUND",
                        "bom_children": [list(r) for r in children],
                    }
                cur.close()
            return out
        if ev.get("mode") == "debug_stock_check":
            from khanal_shared.hana.pool import hana_connection
            out = {}
            with hana_connection() as conn:
                cur = conn.cursor()
                for item_code in ev.get("item_codes") or []:
                    cur.execute(
                        'SELECT "ItemCode", "ItemName", "ManBtchNum", "ManSerNum", "InvntItem", "SellItem", "validFor", "frozenFor" FROM "OITM" WHERE "ItemCode" = ?',
                        [item_code],
                    )
                    itm = cur.fetchone()
                    cur.execute(
                        'SELECT T0."WhsCode", T1."DistNumber", T0."Quantity" '
                        'FROM "OBTQ" T0 JOIN "OBTN" T1 '
                        'ON T0."ItemCode" = T1."ItemCode" AND T0."SysNumber" = T1."SysNumber" '
                        'WHERE T0."ItemCode" = ? AND T0."Quantity" > 0',
                        [item_code],
                    )
                    batches = cur.fetchall() or []
                    cur.execute(
                        'SELECT "WhsCode", "OnHand" FROM "OITW" WHERE "ItemCode" = ?',
                        [item_code],
                    )
                    onhand = cur.fetchall() or []
                    out[item_code] = {
                        "item_master": list(itm) if itm else "NOT FOUND IN OITM",
                        "batches_any_whs": [list(r) for r in batches],
                        "on_hand_by_whs": [list(r) for r in onhand],
                    }
                cur.close()
            return out
        with store.connect() as conn:
            if ev.get("amazon_order_id"):
                return post_invoices.post_one_order(
                    conn, ev["amazon_order_id"], dry_run=bool(ev.get("dry_run"))
                )
            if ev.get("mode") == "retry":
                return post_invoices.retry_failed_orders(conn, limit=int(ev.get("limit") or 50))
            return post_invoices.post_ready_orders(
                conn, limit=int(ev.get("limit") or post_invoices.DEFAULT_LIMIT),
                dry_run=bool(ev.get("dry_run")),
                ship_date_from=ev.get("ship_date_from"), ship_date_to=ev.get("ship_date_to"),
            )

    return _run("post_invoices", _go)


def post_credit_notes_handler(event, context):
    """Default window: last 45 days (Finances PostedDate lags MTR).
    Manual: {"posted_after": ISO, "posted_before": ISO, "dry_run": bool}.
    debug_test_bom_cn: {"mode": "debug_test_bom_cn", "amazon_order_id": ...,
    "seller_sku": ..., "dry_run": bool} — one-off live validation of the
    BOM credit-note fix against a real, already-posted BOM order. Builds a
    synthetic RefundEvent and, unless dry_run, actually posts the CN to
    SAP. Ops tooling only, not on any schedule."""

    def _go():
        ev = event or {}
        if ev.get("mode") == "debug_test_bom_cn":
            from amazon_us import sap_client
            from amazon_us.credit_note_payload import build_credit_note_payload
            from amazon_us.finances_api import RefundEvent, RefundItemAdjustment
            from amazon_us.hana_access import get_bom_children, resolve_acct_code

            oid = ev["amazon_order_id"]
            with store.connect() as conn:
                mapping = store.get_sku_mapping(conn)
                order = store.get_order(conn, oid)
            invoice = sap_client.get_invoice_doc(order["sap_doc_entry"])
            seller_sku = ev["seller_sku"]
            unit_price = next(
                float(l["unit_price"]) for l in order["lines"] if l["seller_sku"] == seller_sku
            )
            refund_event = RefundEvent(
                refund_id=f"{oid}-DEBUGTEST-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
                amazon_order_id=oid,
                posted_date=date.today().isoformat(),
                item_adjustments=[RefundItemAdjustment(seller_sku=seller_sku, principal_amount=-unit_price)],
            )
            payload, skipped = build_credit_note_payload(
                order, refund_event, mapping, order["sap_doc_entry"], invoice.get("DocumentLines") or [],
                resolve_acct_code, get_bom_children,
            )
            if ev.get("dry_run") or payload is None:
                return {"payload": payload, "skipped": skipped}
            result = sap_client.post_credit_note(payload)
            return {"posted": True, "doc_entry": result["doc_entry"], "doc_num": result["doc_num"], "payload": payload}
        if ev.get("mode") == "debug_docdate_vs_purchasedate_scope":
            from khanal_shared.hana.pool import hana_connection
            period_from, period_to = ev["period_from"], ev["period_to"]
            with hana_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    'SELECT "NumAtCard", "DocTotal" FROM "OINV" '
                    'WHERE "CANCELED" = \'N\' AND "DocDate" BETWEEN ? AND ? '
                    'AND "NumAtCard" LIKE \'___-_______-_______\'',
                    [period_from, period_to],
                )
                sap_rows = {str(n).strip(): float(t or 0) for n, t in (cur.fetchall() or [])}
                cur.close()
            with store.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT amazon_order_id, purchase_date, actual_ship_date FROM amazon_us_orders "
                        "WHERE amazon_order_id = ANY(%s)",
                        [list(sap_rows.keys())],
                    )
                    order_rows = {r[0]: (str(r[1]) if r[1] else None, str(r[2]) if r[2] else None) for r in cur.fetchall()}
            not_in_july_purchase = []
            not_found_at_all = []
            for oid, total in sap_rows.items():
                info = order_rows.get(oid)
                if info is None:
                    not_found_at_all.append({"amazon_order_id": oid, "DocTotal": total})
                    continue
                purchase_date, ship_date = info
                purchase_in_july = purchase_date and period_from <= purchase_date[:10] <= period_to
                if not purchase_in_july:
                    not_in_july_purchase.append(
                        {"amazon_order_id": oid, "DocTotal": total, "purchase_date": purchase_date, "actual_ship_date": ship_date}
                    )
            return {
                "sap_total": round(sum(sap_rows.values()), 2),
                "sap_order_count": len(sap_rows),
                "orders_not_found_in_our_db_count": len(not_found_at_all),
                "orders_not_found_in_our_db_total": round(sum(r["DocTotal"] for r in not_found_at_all), 2),
                "orders_with_purchase_date_outside_july_count": len(not_in_july_purchase),
                "orders_with_purchase_date_outside_july_total": round(sum(r["DocTotal"] for r in not_in_july_purchase), 2),
                "sample_outside_july_purchase": not_in_july_purchase[:15],
                "sample_not_found": not_found_at_all[:15],
            }
        if ev.get("mode") == "debug_numatcard_dupes":
            from khanal_shared.hana.pool import hana_connection
            period_from, period_to = ev["period_from"], ev["period_to"]
            with hana_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    'SELECT "NumAtCard", "DocTotal", "DocEntry" FROM "OINV" '
                    'WHERE "CANCELED" = \'N\' AND "DocDate" BETWEEN ? AND ? '
                    'AND "NumAtCard" LIKE \'___-_______-_______\'',
                    [period_from, period_to],
                )
                rows = cur.fetchall() or []
                cur.close()
            by_stripped: dict[str, list] = {}
            for num_at_card, total, doc_entry in rows:
                key = str(num_at_card).strip()
                by_stripped.setdefault(key, []).append(
                    {"raw_num_at_card": repr(str(num_at_card)), "DocTotal": float(total or 0), "DocEntry": int(doc_entry)}
                )
            dupes = {k: v for k, v in by_stripped.items() if len(v) > 1}
            total_all = sum(float(t) for _, t, _ in rows)
            total_by_stripped_key_last_wins = sum(v[-1]["DocTotal"] for v in by_stripped.values())
            return {
                "raw_row_count": len(rows),
                "distinct_raw_numatcard_count": len({str(n).strip() and n for n, _, _ in rows}),
                "distinct_stripped_key_count": len(by_stripped),
                "total_all_rows": round(total_all, 2),
                "total_if_last_wins_per_stripped_key": round(total_by_stripped_key_last_wins, 2),
                "dupe_stripped_keys_count": len(dupes),
                "dupes_sample": dict(list(dupes.items())[:10]),
            }
        if ev.get("mode") == "debug_sap_revenue_scope":
            from khanal_shared.hana.pool import hana_connection
            period_from, period_to = ev["period_from"], ev["period_to"]
            with hana_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    'SELECT SUM("DocTotal"), COUNT(*) FROM "OINV" '
                    'WHERE "CardCode" = ? AND "CANCELED" = \'N\' AND "DocDate" BETWEEN ? AND ?',
                    [constants.SAP_CARD_CODE, period_from, period_to],
                )
                all_row = cur.fetchone()
                cur.execute(
                    'SELECT SUM("DocTotal"), COUNT(*) FROM "OINV" '
                    'WHERE "CardCode" = ? AND "CANCELED" = \'N\' AND "DocDate" BETWEEN ? AND ? '
                    'AND "NumAtCard" LIKE \'___-_______-_______\'',
                    [constants.SAP_CARD_CODE, period_from, period_to],
                )
                pattern_row = cur.fetchone()
                cur.execute(
                    'SELECT "NumAtCard", "DocTotal", "DocEntry", "DocDate" FROM "OINV" '
                    'WHERE "CardCode" = ? AND "CANCELED" = \'N\' AND "DocDate" BETWEEN ? AND ? '
                    'AND NOT ("NumAtCard" LIKE \'___-_______-_______\')',
                    [constants.SAP_CARD_CODE, period_from, period_to],
                )
                non_matching = cur.fetchall() or []
                cur.close()
            return {
                "all_cardcode_total": float(all_row[0] or 0), "all_cardcode_count": int(all_row[1] or 0),
                "order_id_pattern_total": float(pattern_row[0] or 0), "order_id_pattern_count": int(pattern_row[1] or 0),
                "non_matching_docs": [
                    {"NumAtCard": r[0], "DocTotal": float(r[1] or 0), "DocEntry": int(r[2]), "DocDate": str(r[3])}
                    for r in non_matching
                ],
            }
        if ev.get("mode") == "debug_cn_amount_scan":
            """Full-month scan: every real SAP CreditNote's DocTotal (by
            exact NumAtCard = refund_id, not order-level) next to Amazon's
            actual refund event total for that same refund_id -- finds
            every under/over-posted CN, not just a hand-picked sample."""
            from amazon_us import finances_api
            from khanal_shared.hana.pool import hana_connection

            period_from, period_to = ev["period_from"], ev["period_to"]
            cn_to = ev.get("cn_to") or period_to
            with hana_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    'SELECT "NumAtCard", "DocTotal", "DocEntry" FROM "ORIN" '
                    'WHERE "CardCode" = ? AND "CANCELED" = \'N\' AND "DocDate" BETWEEN ? AND ?',
                    [constants.SAP_CARD_CODE, period_from, cn_to],
                )
                sap_docs = {str(n).strip(): (float(t or 0), int(e)) for n, t, e in (cur.fetchall() or [])}
                cur.close()

            events = finances_api.list_refund_events(f"{period_from}T00:00:00Z", f"{period_to}T23:59:59Z")
            mismatches = []
            matched_count = 0
            not_in_sap_count = 0
            for e in events:
                api_total = round(sum(
                    a.principal_amount + a.tax_amount + a.shipping_amount + a.shipping_tax_amount
                    for a in e.item_adjustments
                ), 2)
                sap_entry = sap_docs.get(e.refund_id)
                if sap_entry is None:
                    not_in_sap_count += 1
                    continue
                sap_total, doc_entry = sap_entry
                diff = round(abs(api_total) - sap_total, 2)
                if abs(diff) > 0.01:
                    mismatches.append({
                        "refund_id": e.refund_id, "amazon_order_id": e.amazon_order_id,
                        "api_total": abs(api_total), "sap_total": sap_total,
                        "sap_doc_entry": doc_entry, "diff": diff,
                    })
                else:
                    matched_count += 1
            return {
                "total_refund_events": len(events),
                "matched_exactly": matched_count,
                "not_found_in_sap": not_in_sap_count,
                "mismatches_count": len(mismatches),
                "mismatches_total_diff": round(sum(m["diff"] for m in mismatches), 2),
                "mismatches": mismatches,
            }
        if ev.get("mode") == "debug_refund_event":
            from amazon_us import finances_api
            oids = set(ev.get("amazon_order_ids") or [ev.get("amazon_order_id")])
            events = finances_api.list_refund_events(ev["posted_after"], ev.get("posted_before"))
            matches = [e for e in events if e.amazon_order_id in oids]
            by_order: dict[str, list] = {}
            for e in matches:
                by_order.setdefault(e.amazon_order_id, []).append(
                    {
                        "refund_id": e.refund_id,
                        "posted_date": e.posted_date,
                        "total": round(sum(
                            a.principal_amount + a.tax_amount + a.shipping_amount + a.shipping_tax_amount
                            for a in e.item_adjustments
                        ), 2),
                        "adjustments": [
                            {
                                "seller_sku": a.seller_sku, "principal_amount": a.principal_amount,
                                "tax_amount": a.tax_amount, "shipping_amount": a.shipping_amount,
                                "shipping_tax_amount": a.shipping_tax_amount,
                            }
                            for a in e.item_adjustments
                        ],
                    }
                )
            return {"by_order": by_order}
        if ev.get("mode") == "debug_get_cn_doc":
            from amazon_us import sap_client
            path = f"CreditNotes({int(ev['doc_entry'])})?$select=DocEntry,DocNum,DocTotal,NumAtCard,DocumentLines"
            status, body = sap_client._request("GET", path)
            if status != 200:
                return {"error": f"HTTP {status}", "body": body}
            return {
                "DocTotal": body.get("DocTotal"),
                "NumAtCard": body.get("NumAtCard"),
                "DocumentLines": [
                    {
                        "ItemCode": l.get("ItemCode"), "Quantity": l.get("Quantity"), "UnitPrice": l.get("UnitPrice"),
                        "WithoutInventoryMovement": l.get("WithoutInventoryMovement"),
                        "BaseLine": l.get("BaseLine"), "BaseEntry": l.get("BaseEntry"), "TreeType": l.get("TreeType"),
                    }
                    for l in (body.get("DocumentLines") or [])
                ],
            }

        posted_after = ev.get("posted_after") or (
            datetime.now(timezone.utc) - timedelta(days=45)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with store.connect() as conn:
            return post_credit_notes.post_refunds(
                conn, posted_after, ev.get("posted_before"), dry_run=bool(ev.get("dry_run"))
            )

    return _run("post_credit_notes", _go)


def recon_handler(event, context):
    """modes: invariants (default) | mtr | close_stuck_run.
    mtr event: {"mode": "mtr", "s3_bucket": ..., "s3_key": ...,
    "period_from": "YYYY-MM-DD", "period_to": "YYYY-MM-DD"}.
    close_stuck_run event: {"mode": "close_stuck_run", "run_id": int,
    "status": "error"?, "error": str?} -- a handler that dies via hard
    timeout (Sandbox.Timedout) never reaches finish_sync_log, leaving its
    amazon_us_sync_log row permanently stuck at status='running'. This
    closes it out manually; status defaults to "error"."""
    mode = (event or {}).get("mode", "invariants")

    def _go():
        with store.connect() as conn:
            if mode == "invariants":
                return recon.run_invariants(conn)
            if mode == "mtr":
                return recon.run_mtr_recon(
                    conn, event["s3_bucket"], event["s3_key"], event["period_from"], event["period_to"]
                )
            if mode == "close_stuck_run":
                run_id = int(event["run_id"])
                store.finish_sync_log(
                    conn, run_id, event.get("status", "error"), error=event.get("error")
                )
                return {"closed_run_id": run_id}
            raise ValueError(f"Unknown recon mode: {mode}")

    return _run(f"recon[{mode}]", _go)


# ── Dashboard HTTP API (read-only + a couple of manual-retry actions) ──────

def _json_response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _http_method(event: dict[str, Any]) -> str:
    http = (event.get("requestContext") or {}).get("http") or {}
    return str(http.get("method") or event.get("httpMethod") or "GET").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _query_params(event: dict[str, Any]) -> dict[str, str]:
    params = event.get("queryStringParameters") or {}
    return {str(k): str(v) for k, v in params.items() if v is not None} if isinstance(params, dict) else {}


def _invoke_async(function_name_suffix: str, payload: dict) -> None:
    """Fire-and-forget invoke of another function in this service. API
    Gateway HTTP APIs hard-cap integration timeout at 30s, so anything that
    can run long (order sync, FBA sync, a full post/recon pass) must be
    handed off to its own 900s-timeout function rather than run in-process
    here. Progress/results show up in Sync History, not in the response."""
    import os

    import boto3

    function_name = f"amazon-us-b2c-{os.environ['STAGE']}-{function_name_suffix}"
    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(payload).encode("utf-8"),
    )


def dashboard_handler(event, context):
    method = _http_method(event)
    if method == "OPTIONS":
        return _json_response(200, {"ok": True})

    path = _path(event)
    q = _query_params(event)

    try:
        if path.endswith("/v1/amazon/sync-orders") and method == "POST":
            mode = q.get("mode") or "incremental"
            payload = {"mode": mode}
            if q.get("from_date"):
                payload["from_date"] = q["from_date"]
            if q.get("to_date"):
                payload["to_date"] = q["to_date"]
            _invoke_async("amazonFetch", payload)
            return _json_response(202, {"triggered": "amazonFetch", "mode": mode})

        if path.endswith("/v1/amazon/sync-fba") and method == "POST":
            payload = {}
            if q.get("from_date"):
                payload["from_date"] = q["from_date"]
            if q.get("to_date"):
                payload["to_date"] = q["to_date"]
            _invoke_async("amazonFbaShipSync", payload)
            return _json_response(202, {"triggered": "amazonFbaShipSync"})

        if path.endswith("/v1/amazon/post-ready") and method == "POST":
            payload = {"limit": int(q.get("limit") or post_invoices.DEFAULT_LIMIT)}
            if q.get("from_date"):
                payload["ship_date_from"] = q["from_date"]
            if q.get("to_date"):
                payload["ship_date_to"] = q["to_date"]
            _invoke_async("amazonPostInvoices", payload)
            return _json_response(202, {"triggered": "amazonPostInvoices"})

        if path.endswith("/v1/amazon/run-recon") and method == "POST":
            mode = q.get("mode") or "invariants"
            payload = {"mode": mode}
            for key in ("s3_bucket", "s3_key", "period_from", "period_to"):
                if q.get(key):
                    payload[key] = q[key]
            _invoke_async("amazonRecon", payload)
            return _json_response(202, {"triggered": "amazonRecon", "mode": mode})

        if path.endswith("/v1/amazon/export-recon") and method == "POST":
            period_from = (q.get("period_from") or "").strip()
            period_to = (q.get("period_to") or "").strip()
            if not period_from or not period_to:
                return _json_response(400, {"error": "period_from and period_to are required"})
            _invoke_async("amazonPostInvoices", {"mode": "export_recon", "period_from": period_from, "period_to": period_to})
            return _json_response(202, {"triggered": "amazonPostInvoices", "mode": "export_recon"})

        if path.endswith("/v1/amazon/export-finance-revenue") and method == "POST":
            period_from = (q.get("period_from") or "").strip()
            period_to = (q.get("period_to") or "").strip()
            if not period_from or not period_to:
                return _json_response(400, {"error": "period_from and period_to are required"})
            _invoke_async("amazonPostInvoices", {"mode": "export_finance_revenue_report", "period_from": period_from, "period_to": period_to})
            return _json_response(202, {"triggered": "amazonPostInvoices", "mode": "export_finance_revenue_report"})

        if path.endswith("/v1/amazon/retry-invoices") and method == "POST":
            with store.connect() as conn:
                limit = int(q.get("limit") or 50)
                return _json_response(200, post_invoices.retry_failed_orders(conn, limit=limit))

        if path.endswith("/v1/amazon/retry-cn") and method == "POST":
            with store.connect() as conn:
                posted_after = q.get("posted_after") or (
                    datetime.now(timezone.utc) - timedelta(days=45)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                dry_run = (q.get("dry_run") or "").lower() in ("1", "true")
                return _json_response(
                    200,
                    post_credit_notes.post_refunds(
                        conn, posted_after, q.get("posted_before"), dry_run=dry_run
                    ),
                )

        if path.endswith("/v1/amazon/recon-download") and method == "GET":
            key = (q.get("key") or "").strip()
            if not key.startswith("recon/"):
                return _json_response(400, {"error": "invalid key"})
            import boto3

            s3 = boto3.client("s3")
            url = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": constants.RECON_BUCKET, "Key": key},
                ExpiresIn=300,
            )
            return _json_response(200, {"url": url})

        if path.endswith("/v1/amazon/order-detail") and not (q.get("amazon_order_id") or "").strip():
            return _json_response(400, {"error": "amazon_order_id is required"})

        with store.connect() as conn:
            if path.endswith("/v1/amazon/stats"):
                return _json_response(200, dashboard_queries.fetch_stats(conn))

            if path.endswith("/v1/amazon/orders"):
                limit = dashboard_queries._clamp_limit(q.get("limit"))
                offset = dashboard_queries._clamp_offset(q.get("offset"))
                return _json_response(
                    200,
                    dashboard_queries.fetch_orders(
                        conn,
                        limit=limit,
                        offset=offset,
                        stage=q.get("stage") or None,
                        amazon_order_id=(q.get("amazon_order_id") or "").strip() or None,
                        order_date_from=q.get("order_date_from") or None,
                        order_date_to=q.get("order_date_to") or None,
                        ship_country=q.get("ship_country") or None,
                        sort_by=q.get("sort_by") or None,
                        sort_dir=q.get("sort_dir") or None,
                    ),
                )

            if path.endswith("/v1/amazon/order-detail"):
                oid = (q.get("amazon_order_id") or "").strip()
                detail = dashboard_queries.fetch_order_detail(conn, oid)
                if detail["header"] is None:
                    return _json_response(404, {"error": "Order not found", "amazon_order_id": oid})
                return _json_response(200, detail)

            if path.endswith("/v1/amazon/invoice-failures"):
                limit = dashboard_queries._clamp_limit(q.get("limit"), default=50)
                return _json_response(200, dashboard_queries.fetch_invoice_failures(conn, limit=limit))

            if path.endswith("/v1/amazon/cn-log"):
                limit = dashboard_queries._clamp_limit(q.get("limit"))
                return _json_response(
                    200, dashboard_queries.fetch_cn_log(conn, limit=limit, status=q.get("status") or None)
                )

            if path.endswith("/v1/amazon/sync-log"):
                limit = dashboard_queries._clamp_limit(q.get("limit"), default=20)
                items = dashboard_queries.fetch_sync_log(conn, limit=limit, mode=q.get("mode") or None)
                return _json_response(200, {"items": items})

        return _json_response(404, {"error": "Not found", "path": path})
    except Exception as exc:
        scrubbed = _scrub(str(exc))
        logger.error("dashboard_api_error path=%s error=%s", path, scrubbed)
        return _json_response(500, {"error": scrubbed})


def _export_recon(period_from: str, period_to: str, s3_bucket: str) -> dict:
    """Order-level reconciliation export: our staged (API-side) data next
    to the live SAP-side aggregate from HANA, for invoices and credit
    notes. Two CSVs written to S3 — a bulk single-query export, not a
    per-order loop (this is meant for 10k+ row windows).

    Invoices window: SAP recognizes revenue by DocDate (= actual_ship_date,
    the posting pipeline's own rule — matches how Amazon's MTR indexes
    revenue too), not purchase_date. An order purchased in June but
    shipped in July contributes real July SAP revenue. Windowing only by
    purchase_date silently drops those from the comparison — confirmed
    2026-08-11 against a real Finance-reported diff (737 such orders,
    exactly accounting for the gap to the cent). So this includes any
    order where EITHER purchase_date OR actual_ship_date falls in the
    window, and tags which one(s) matched via `in_window_by` — filter/sum
    by `actual_ship_date` alone to match SAP's real period total exactly.

    Credit notes: sap_order_refund_total_in_window is an ORDER-level total
    (HANA/SAP has no per-refund-event key to split it by) — populated only
    on the first row per order, blank on the rest, specifically so a naive
    spreadsheet SUM() can't double-count an order with multiple refund
    events (this bit Finance for real on 2026-08-11, off by exactly the
    duplicated orders' total)."""
    import csv
    import io

    import boto3

    from amazon_us import finances_api

    sap_side = recon.fetch_sap_side(period_from, period_to)

    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT o.amazon_order_id, o.order_status, o.stage, o.purchase_date, o.actual_ship_date,
                       o.ship_city, o.ship_state, o.ship_country, o.order_total_amount,
                       o.sap_doc_entry, o.sap_doc_num, o.posted_at, o.last_error,
                       COALESCE(SUM(l.item_price_amount), 0), COALESCE(SUM(l.item_tax_amount), 0),
                       COALESCE(SUM(l.shipping_price_amount), 0), COALESCE(SUM(l.shipping_tax_amount), 0)
                FROM amazon_us_orders o
                LEFT JOIN amazon_us_order_lines l ON l.amazon_order_id = o.amazon_order_id
                WHERE (o.purchase_date >= %s AND o.purchase_date < %s::date + interval '1 day')
                   OR (o.actual_ship_date >= %s::date AND o.actual_ship_date <= %s::date)
                GROUP BY o.amazon_order_id, o.order_status, o.stage, o.purchase_date, o.actual_ship_date,
                         o.ship_city, o.ship_state, o.ship_country, o.order_total_amount,
                         o.sap_doc_entry, o.sap_doc_num, o.posted_at, o.last_error
                ORDER BY o.actual_ship_date NULLS LAST, o.purchase_date
                """,
                [period_from, period_to, period_from, period_to],
            )
            order_rows = cur.fetchall()

    inv_buf = io.StringIO()
    w = csv.writer(inv_buf)
    w.writerow([
        "amazon_order_id", "order_status", "stage", "purchase_date", "actual_ship_date", "in_window_by",
        "ship_city", "ship_state", "ship_country",
        "api_item_price", "api_item_tax", "api_shipping", "api_shipping_tax",
        "api_computed_total", "api_order_total_field",
        "sap_doc_entry", "sap_doc_num", "posted_at",
        "sap_actual_doctotal", "gross_diff",
        "last_error",
    ])
    for (oid, status, stage, purchase_date, ship_date, city, state, country, order_total,
         doc_entry, doc_num, posted_at, last_error,
         item_price, item_tax, shipping, shipping_tax) in order_rows:
        purchase_in_window = purchase_date and period_from <= str(purchase_date)[:10] < (
            (datetime.strptime(period_to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        )
        ship_in_window = ship_date and period_from <= str(ship_date)[:10] <= period_to
        in_window_by = "+".join(
            label for label, matched in (("purchase_date", purchase_in_window), ("ship_date", ship_in_window)) if matched
        )
        api_computed_total = float(item_price) + float(item_tax) + float(shipping) + float(shipping_tax)
        sap_total = sap_side.get(oid, {}).get("sales")
        gross_diff = round(api_computed_total - sap_total, 2) if sap_total is not None else ""
        w.writerow([
            oid, status, stage, purchase_date, ship_date, in_window_by, city, state, country,
            item_price, item_tax, shipping, shipping_tax,
            round(api_computed_total, 2), order_total,
            doc_entry or "", doc_num or "", posted_at or "",
            sap_total if sap_total is not None else "", gross_diff,
            last_error or "",
        ])

    events = finances_api.list_refund_events(f"{period_from}T00:00:00Z", f"{period_to}T23:59:59Z")
    with store.connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT refund_id, status, sap_doc_entry, sap_doc_num, base_doc_entry, skip_reason, error_message "
                "FROM amazon_us_cn_log WHERE refund_id = ANY(%s)",
                [[e.refund_id for e in events]],
            )
            cn_rows = {r[0]: r[1:] for r in cur.fetchall()}

    cn_buf = io.StringIO()
    w2 = csv.writer(cn_buf)
    w2.writerow([
        "refund_id", "amazon_order_id", "posted_date",
        "api_principal", "api_item_tax", "api_shipping", "api_shipping_tax", "api_total",
        "cn_status", "sap_doc_entry", "sap_doc_num", "base_doc_entry",
        "sap_order_refund_total_in_window", "skip_reason", "error_message",
    ])
    seen_orders_for_sap_refund_total: set[str] = set()
    for e in events:
        principal = sum(a.principal_amount for a in e.item_adjustments)
        item_tax = sum(a.tax_amount for a in e.item_adjustments)
        ship = sum(a.shipping_amount for a in e.item_adjustments)
        ship_tax = sum(a.shipping_tax_amount for a in e.item_adjustments)
        total = principal + item_tax + ship + ship_tax
        cn = cn_rows.get(e.refund_id)
        status, doc_entry, doc_num, base_entry, skip_reason, err = cn if cn else ("", "", "", "", "", "")
        # Only the first row per order carries the order-level SAP total —
        # blank on the rest, so SUM()-ing this column in a spreadsheet
        # can't double-count an order with multiple refund events.
        sap_refund_total = ""
        if e.amazon_order_id not in seen_orders_for_sap_refund_total:
            seen_orders_for_sap_refund_total.add(e.amazon_order_id)
            val = sap_side.get(e.amazon_order_id, {}).get("refunds")
            sap_refund_total = val if val is not None else ""
        w2.writerow([
            e.refund_id, e.amazon_order_id, e.posted_date,
            round(principal, 2), round(item_tax, 2), round(ship, 2), round(ship_tax, 2), round(total, 2),
            status, doc_entry or "", doc_num or "", base_entry or "",
            sap_refund_total, skip_reason or "", err or "",
        ])

    s3 = boto3.client("s3")
    inv_key = f"recon-export/invoices_{period_from}_{period_to}.csv"
    cn_key = f"recon-export/credit_notes_{period_from}_{period_to}.csv"
    s3.put_object(Bucket=s3_bucket, Key=inv_key, Body=inv_buf.getvalue().encode("utf-8"))
    s3.put_object(Bucket=s3_bucket, Key=cn_key, Body=cn_buf.getvalue().encode("utf-8"))
    return {
        "invoices_key": inv_key, "credit_notes_key": cn_key,
        "invoice_rows": len(order_rows), "cn_rows": len(events),
    }


def _export_finance_revenue_report(period_from: str, period_to: str, s3_bucket: str) -> dict:
    """Runs Finance's own Revenue Recognition SQL (recon.FINANCE_REVENUE_QUERY,
    pasted from their SAP HANA report, unmodified) scoped to Amazon US B2C
    (CardCode C00001), writes it to S3 line-for-line as Finance would pull
    it themselves. Exists so "does our revenue match Finance's report" is
    answered by running their exact query, not by us re-deriving Taxable
    Value / Freight / COGS and hoping the formulas agree."""
    import csv
    import io

    import boto3

    rows = recon.fetch_finance_revenue_rows(period_from, period_to)

    buf = io.StringIO()
    fieldnames = list(rows[0].keys()) if rows else ["Document Date"]
    w = csv.DictWriter(buf, fieldnames=fieldnames)
    w.writeheader()
    w.writerows(rows)

    key = f"recon-export/finance_revenue_report_{period_from}_{period_to}.csv"
    s3 = boto3.client("s3")
    s3.put_object(Bucket=s3_bucket, Key=key, Body=buf.getvalue().encode("utf-8"))

    net_taxable_total = round(sum(float(r.get("Net Taxable Value") or 0) for r in rows), 2)
    taxable_total = round(sum(float(r.get("Taxable Value") or 0) for r in rows), 2)
    return {
        "key": key,
        "rows": len(rows),
        "net_taxable_value_total": net_taxable_total,
        "taxable_value_total": taxable_total,
    }

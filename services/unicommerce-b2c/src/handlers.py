"""Lambda entrypoints. Thin: parse event -> open DB -> run worker -> report.

Every handler alerts via SES on an unhandled crash and re-raises so the
invocation is marked failed in CloudWatch (EventBridge metrics stay
honest) -- same pattern as amazon-us-b2c's handlers.py.

fetchOrders, postInvoices, and postCreditNotes are implemented so far --
see docs/migration/03-unicommerce-b2c.md for the full 7-function plan
(PO/GRN, inventory sync, recon, dashboard API still to come).
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from pathlib import Path

# Lambda package root is ``src/``; local modules use ``from unicommerce.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from unicommerce import credit_notes, fetch_sync, post_invoices, store
from unicommerce.alerts import send_alert

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# DATABASE_URL embeds a password; some driver errors echo the DSN back in
# the exception text, so scrub it before any crash detail goes out in an
# alert email (same precaution as amazon-us-b2c's handlers.py).
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
    """{"mode": "orders"|"returns" (default "orders"), "max_orders"|"max_returns": int?,
    "from_date": "YYYY-MM-DD"?, "to_date": "YYYY-MM-DD"?, "channel_name": str? (orders only)}.
    from_date present -> explicit backfill window, cursor not advanced.
    Omitted -> cursor-driven incremental fetch. One handler covers both
    UC fetch flows (mirrors amazonFetch's mode input)."""
    ev = event or {}
    mode = (ev.get("mode") or "orders").strip().lower()

    def _go_orders():
        with store.connect() as conn:
            return fetch_sync.fetch_orders(
                conn,
                max_orders=ev.get("max_orders"),
                from_date=ev.get("from_date"),
                to_date=ev.get("to_date"),
                channel_name=ev.get("channel_name"),
            )

    def _go_returns():
        with store.connect() as conn:
            return fetch_sync.fetch_returns(
                conn,
                max_returns=ev.get("max_returns") or ev.get("max_orders"),
                from_date=ev.get("from_date"),
                to_date=ev.get("to_date"),
            )

    if mode == "returns":
        return _run("fetch_returns", _go_returns)
    return _run("fetch_orders", _go_orders)


def post_invoices_handler(event, context):
    """{"limit": 50?, "channel_name": str?, "start_date": "YYYY-MM-DD"?,
    "end_date": "YYYY-MM-DD"?} — bulk posting run (promote fetched->ready +
    recovery + verify + ready-order posting, same as the scheduled cron;
    the date range keys off the order_date, matching the Ops Center UX).
    {"uniware_id": str} — manual single-order retry, bypassing the limit."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            if ev.get("uniware_id"):
                return post_invoices.post_one_order(conn, ev["uniware_id"])
            return post_invoices.post_ready_orders(
                conn,
                limit=int(ev.get("limit") or 50),
                channel_name=ev.get("channel_name"),
                start_date=ev.get("start_date"),
                end_date=ev.get("end_date"),
            )

    return _run("post_invoices", _go)


def post_credit_notes_handler(event, context):
    """{"limit": 50?, "start_date": "YYYY-MM-DD"?, "end_date": "YYYY-MM-DD"?,
    "channel_name": str?} — bulk posting run, date range keys off the
    parent order's order_date (same UX as the Ops Center date picker).
    {"uc_return_id": str} — manual single-return retry, bypassing the limit."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            if ev.get("uc_return_id"):
                return credit_notes.post_one_credit_note(conn, ev["uc_return_id"])
            return credit_notes.post_credit_notes(
                conn,
                limit=int(ev.get("limit") or 50),
                start_date=ev.get("start_date"),
                end_date=ev.get("end_date"),
                channel_name=ev.get("channel_name"),
            )

    return _run("post_credit_notes", _go)

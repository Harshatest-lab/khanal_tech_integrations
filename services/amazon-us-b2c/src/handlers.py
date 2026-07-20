"""Lambda entrypoints. Thin: parse event -> open DB -> run worker -> report.

Every handler alerts via SES on an unhandled crash and re-raises so the
invocation is marked failed in CloudWatch (EventBridge metrics stay
honest).
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Lambda package root is ``src/``; local modules use ``from amazon_us.*``.
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from amazon_us import fetch_sync, post_credit_notes, post_invoices, recon, store
from amazon_us.alerts import send_alert
from amazon_us.fba_ship_sync import sync_ship_dates

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def _run(label: str, fn):
    try:
        result = fn()
        logger.info("%s result: %s", label, result)
        return result
    except Exception as e:
        send_alert(f"{label} crashed", f"{e}\n\n{traceback.format_exc()[-3000:]}")
        raise


def fetch_handler(event, context):
    """modes: incremental (default) | report | backfill.
    report/backfill events: {"mode": "report", "from_date": "YYYY-MM-DD",
    "to_date": "YYYY-MM-DD", "lookback_days": 30?}"""
    mode = (event or {}).get("mode", "incremental")

    def _go():
        with store.connect() as conn:
            if mode == "incremental":
                return fetch_sync.sync_incremental(conn)
            if mode in ("report", "backfill"):
                from_date = event.get("from_date") or date.today().replace(day=1).isoformat()
                to_date = event.get("to_date") or date.today().isoformat()
                return fetch_sync.sync_via_report(conn, from_date, to_date, event.get("lookback_days"))
            raise ValueError(f"Unknown fetch mode: {mode}")

    return _run(f"fetch[{mode}]", _go)


def fba_ship_sync_handler(event, context):
    """Default window: last 21 days (Pacific). Manual: {"from_date", "to_date"}."""

    def _go():
        from_date = (event or {}).get("from_date") or (date.today() - timedelta(days=21)).isoformat()
        to_date = (event or {}).get("to_date") or date.today().isoformat()
        with store.connect() as conn:
            return sync_ship_dates(conn, from_date, to_date)

    return _run("fba_ship_sync", _go)


def post_invoices_handler(event, context):
    """{"limit": 200?, "dry_run": false?, "mode": "post"|"retry"}"""

    def _go():
        ev = event or {}
        with store.connect() as conn:
            if ev.get("mode") == "retry":
                return post_invoices.retry_failed_orders(conn, limit=int(ev.get("limit") or 50))
            return post_invoices.post_ready_orders(
                conn, limit=int(ev.get("limit") or post_invoices.DEFAULT_LIMIT),
                dry_run=bool(ev.get("dry_run")),
            )

    return _run("post_invoices", _go)


def post_credit_notes_handler(event, context):
    """Default window: last 45 days (Finances PostedDate lags MTR).
    Manual: {"posted_after": ISO, "posted_before": ISO, "dry_run": bool}."""

    def _go():
        ev = event or {}
        posted_after = ev.get("posted_after") or (
            datetime.now(timezone.utc) - timedelta(days=45)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        with store.connect() as conn:
            return post_credit_notes.post_refunds(
                conn, posted_after, ev.get("posted_before"), dry_run=bool(ev.get("dry_run"))
            )

    return _run("post_credit_notes", _go)


def recon_handler(event, context):
    """modes: invariants (default) | mtr.
    mtr event: {"mode": "mtr", "s3_bucket": ..., "s3_key": ...,
    "period_from": "YYYY-MM-DD", "period_to": "YYYY-MM-DD"}."""
    mode = (event or {}).get("mode", "invariants")

    def _go():
        with store.connect() as conn:
            if mode == "invariants":
                return recon.run_invariants(conn)
            if mode == "mtr":
                return recon.run_mtr_recon(
                    conn, event["s3_bucket"], event["s3_key"], event["period_from"], event["period_to"]
                )
            raise ValueError(f"Unknown recon mode: {mode}")

    return _run(f"recon[{mode}]", _go)

"""Lambda entrypoints. Thin: parse event -> open DB -> run worker -> report.

Every handler alerts via SES on an unhandled crash and re-raises so the
invocation is marked failed in CloudWatch (EventBridge metrics stay
honest) -- same pattern as amazon-us-b2c's handlers.py.

Only fetchOrders is implemented so far -- see docs/migration/03-unicommerce-b2c.md
for the full 7-function plan (post invoices, post credit notes, PO/GRN,
inventory sync, recon, dashboard API still to come).
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

from unicommerce import fetch_sync, store
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
    """{"max_orders": int?, "from_date": "YYYY-MM-DD"?, "to_date": "YYYY-MM-DD"?,
    "channel_name": str?}. from_date present -> explicit backfill window,
    cursor not advanced. Omitted -> cursor-driven incremental fetch."""
    ev = event or {}

    def _go():
        with store.connect() as conn:
            return fetch_sync.fetch_orders(
                conn,
                max_orders=ev.get("max_orders"),
                from_date=ev.get("from_date"),
                to_date=ev.get("to_date"),
                channel_name=ev.get("channel_name"),
            )

    return _run("fetch_orders", _go)

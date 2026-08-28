"""Lambda entrypoints. Thin: parse event -> run the module logic -> report.

Same shape as amazon-us-b2c/src/handlers.py: a single mode-dispatching
handler per scheduled function, no business logic in this file.
"""

from __future__ import annotations

import logging
import sys
import traceback
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from safexpress import pod_service  # noqa: E402
from safexpress.config import DEFAULT_BATCH_SIZE, DEFAULT_LOOKBACK_DAYS  # noqa: E402

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def _run(label: str, fn):
    try:
        result = fn()
        logger.info("%s result: %s", label, result)
        return result
    except Exception:
        logger.error("%s crashed: %s", label, traceback.format_exc())
        raise


def track_handler(event, context):
    """modes: poll (default) | retry_pending_sap | backfill.
    poll event: {"days": int?, "batch_size": int?} — small HANA window for
    new-invoice discovery, then works the Postgres backlog (all open rows,
    not just freshly-discovered ones) end-to-end (tracking -> POD -> SAP
    PATCH), newest-invoice-first with a small oldest-first reserve, capped
    at batch_size rows.
    retry_pending_sap event: {"limit": int?} — retries only the SAP PATCH
    for rows that already have a POD uploaded but a previously failed
    write; no fresh HANA/SafeExpress calls.
    backfill event: {} — one-time go-live cutover only, run by hand once:
    unbounded HANA scan (no date filter) seeding every currently-untracked
    SafeExpress invoice into the backlog. Does not process anything itself;
    subsequent poll runs work the seeded backlog down."""
    ev = event or {}
    mode = ev.get("mode", "poll")

    def _go():
        if mode == "retry_pending_sap":
            return pod_service.retry_pending_sap(limit=int(ev.get("limit") or DEFAULT_BATCH_SIZE))
        if mode == "poll":
            return pod_service.poll_and_process(
                days=int(ev.get("days") or DEFAULT_LOOKBACK_DAYS),
                batch_size=int(ev.get("batch_size") or DEFAULT_BATCH_SIZE),
            )
        if mode == "backfill":
            return pod_service.seed_backfill()
        raise ValueError(f"Unknown track mode: {mode}")

    return _run(f"track[{mode}]", _go)

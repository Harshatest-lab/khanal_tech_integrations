"""Business constants for the Unicommerce B2C posting pipeline."""

from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# Orders/returns dated before this were either handled outside this
# pipeline or predate its go-live -- posting them now would inject
# back-dated transactions into live SAP after the books have moved on.
# Same pattern as amazon_us.constants.POSTING_CUTOFF_DATE. Enforced as a
# hard floor in store.get_ready_orders / store.get_pending_credit_notes,
# not just an optional dashboard date-range filter, so a batch retry (or
# the scheduled cron, once enabled) can never sweep in a pre-cutoff row
# even if nobody remembers to pass a date range.
POSTING_CUTOFF_DATE = _env("UC_POSTING_CUTOFF_DATE", "2026-08-01")

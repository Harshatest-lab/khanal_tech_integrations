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

# Hard floor on StockTransfers.DocDate for pogrn.fetch_new_transfers -- the
# watermark alone isn't enough: it started near 0 at PoGrn's 2026-08-20
# go-live, so a plain DocEntry-ascending scan walks SAP's *entire*
# StockTransfers history, including years-old cancelled/reversed transfers
# no longer relevant to anything. Confirmed live 2026-08-24: DocEntry 2966
# (a 2022-05-25 cancellation reversal, all-negative lines) got staged and
# has retried hourly since at least 2026-08-22, permanently failing
# "No valid SKU/quantity" since aggregate_po_items/build_grn_items both
# drop non-positive quantities -- and its *original* transfer (DocEntry
# 2923, same date, positive lines) almost certainly got mirrored into UC
# as a real PO+GRN despite SAP having cancelled it back in 2022. Floored
# at go-live date: PoGrn has no reason to mirror anything older into UC.
POGRN_DOC_DATE_FLOOR = _env("UC_POGRN_DOC_DATE_FLOOR", "2026-08-20")

"""Fetch refunds via the Finances API (/finances/2024-06-19/transactions).

No server-side event-type filter (confirmed live: passing transactionType
is silently ignored) — filtered client-side from the flat transactions
list. Only transactionType 'Refund' is kept (FBAInventoryReimbursement not
needed right now).

Only transactionStatus 'RELEASED' (final settlement) is kept. Amazon posts
most refunds twice — once as 'DEFERRED_RELEASED' (provisional, same
refund_id/amount) days before the final 'RELEASED' posting in a later
settlement cycle. Keeping both would double-count real money. A refund
still awaiting its final posting simply won't appear yet and gets picked
up by a later incremental sync once it settles.
"""

import time

from amazon_uk import applog
from amazon_uk import sp_api

log = applog.get_logger("amz_refunds")

# No documented rate limit found yet for this newer endpoint; reusing the
# old Finances API's pacing (0.5 req/s) as a safe default until a real 429
# says otherwise.
PAGE_PACING_SECONDS = 2.2

MAX_RESULTS_PER_PAGE = 500  # confirmed live max, vs 100 on the old API

WANTED_TRANSACTION_TYPES = {"Refund"}
WANTED_TRANSACTION_STATUS = "RELEASED"


def fetch_refunds(posted_after: str, posted_before: str) -> list[dict]:
    """All final-settlement (RELEASED) Refund transactions POSTED in
    [posted_after, posted_before).

    Dates are ISO 8601 strings, e.g. '2026-08-01T00:00:00Z'.
    """
    params = {
        "postedAfter": posted_after,
        "postedBefore": posted_before,
        "pageSize": MAX_RESULTS_PER_PAGE,
    }

    wanted = []
    page = 1
    while True:
        payload = sp_api.request("GET", "/finances/2024-06-19/transactions", params)
        body = payload.get("payload", {}) or {}
        all_txns = body.get("transactions") or []
        page_wanted = [t for t in all_txns
                       if t.get("transactionType") in WANTED_TRANSACTION_TYPES
                       and t.get("transactionStatus") == WANTED_TRANSACTION_STATUS]
        wanted += page_wanted
        log.info(f"page {page}: {len(page_wanted)}/{len(all_txns)} refund transactions "
                 f"(total so far: {len(wanted)})")

        next_token = body.get("nextToken")
        if not next_token:
            return wanted
        params = {"nextToken": next_token}
        page += 1
        time.sleep(PAGE_PACING_SECONDS)

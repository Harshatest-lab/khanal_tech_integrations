"""One daily summary email: fetched / posted / failed counts for the last 24 h.

Read-only over the tables the jobs already write (fetch_log, sap_postings,
sap_credit_notes), so it needs no state of its own and still reports when a
job didn't run (its counts are just 0).
"""

from __future__ import annotations

import datetime as _dt

from amazon_uk import alerts, store

WINDOW_HOURS = 24


def _one(cur, sql: str) -> int:
    cur.execute(sql)
    return int(cur.fetchone()[0] or 0)


def collect_counts(hours: int = WINDOW_HOURS) -> dict:
    w = f"now() - interval '{int(hours)} hours'"
    conn = store.connect()
    try:
        with conn.cursor() as cur:
            return {
                "orders_fetched": _one(cur, f"SELECT COALESCE(SUM(orders_fetched),0) FROM amazonuk.fetch_log WHERE source='orders' AND fetched_at >= {w}"),
                "refunds_fetched": _one(cur, f"SELECT COALESCE(SUM(orders_fetched),0) FROM amazonuk.fetch_log WHERE source='refunds' AND fetched_at >= {w}"),
                "invoices_posted": _one(cur, f"SELECT COUNT(*) FROM amazonuk.sap_postings WHERE status='posted' AND posted_at >= {w}"),
                "invoices_failed": _one(cur, f"SELECT COUNT(*) FROM amazonuk.sap_postings WHERE status='failed' AND posted_at >= {w}"),
                "credit_notes_posted": _one(cur, f"SELECT COUNT(*) FROM amazonuk.sap_credit_notes WHERE status='posted' AND posted_at >= {w}"),
                "credit_notes_failed": _one(cur, f"SELECT COUNT(*) FROM amazonuk.sap_credit_notes WHERE status='failed' AND posted_at >= {w}"),
                "invoices_dead": _one(cur, "SELECT COUNT(*) FROM amazonuk.sap_postings WHERE dead"),
                "credit_notes_dead": _one(cur, "SELECT COUNT(*) FROM amazonuk.sap_credit_notes WHERE dead"),
                "invoices_failed_total": _one(cur, "SELECT COUNT(*) FROM amazonuk.sap_postings WHERE status='failed'"),
                "credit_notes_failed_total": _one(cur, "SELECT COUNT(*) FROM amazonuk.sap_credit_notes WHERE status='failed'"),
            }
    finally:
        conn.close()


def build(counts: dict, hours: int = WINDOW_HOURS) -> tuple[str, str]:
    c = counts
    failed = c["invoices_failed"] + c["credit_notes_failed"]
    subject = (
        f"Daily report: {c['orders_fetched']} fetched, "
        f"{c['invoices_posted'] + c['credit_notes_posted']} posted, {failed} failed"
    )
    body = "\n".join([
        f"Amazon UK daily report (last {hours} h, {_dt.datetime.now(_dt.timezone.utc):%Y-%m-%d %H:%M} UTC)",
        "",
        f"Fetched:  orders={c['orders_fetched']}, refunds={c['refunds_fetched']}",
        f"Posted:   invoices={c['invoices_posted']}, credit notes={c['credit_notes_posted']}",
        f"Failed:   invoices={c['invoices_failed']}, credit notes={c['credit_notes_failed']}",
        "",
        f"Still failed overall (retried each run): invoices={c['invoices_failed_total']}, "
        f"credit notes={c['credit_notes_failed_total']}",
        f"Dead (gave up after {store.MAX_POST_ATTEMPTS} attempts, not retried): invoices={c['invoices_dead']}, "
        f"credit notes={c['credit_notes_dead']}",
    ])
    return subject, body


def run() -> dict:
    counts = collect_counts()
    subject, body = build(counts)
    counts["emailed"] = alerts.send_daily_report(body, subject)
    return counts

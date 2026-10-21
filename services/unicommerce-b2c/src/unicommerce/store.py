"""Postgres persistence for the Unicommerce B2C pipeline (psycopg, repo style).

Only the fetch worker's needs are implemented so far -- see
docs/migration/03-unicommerce-b2c.md for the full 7-function plan.
"""

from __future__ import annotations

import json
import logging

import psycopg

from unicommerce import STATUS_OVERWRITABLE_ON_REFETCH, STATUS_SKIP_ON_REFETCH
from unicommerce.credentials import DATABASE_URL
from unicommerce.mappers import build_line_items, extract_channel, extract_order_scalar_fields
from unicommerce.dates import extract_order_date

logger = logging.getLogger(__name__)


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def get_order_status(conn: psycopg.Connection, uniware_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT pipeline_status FROM unicommerce_orders WHERE uniware_id = %s", (uniware_id,))
        row = cur.fetchone()
    return row[0] if row else None


def upsert_order(conn: psycopg.Connection, uniware_id: str, order: dict) -> str:
    """Insert or refresh one staged order + its lines.

    Matches khanal_tech_integrations/unicommerce/workers/fetch.py exactly
    (not the amazon_us-style "protected stages" rule) for dual-run
    comparability:
      - pipeline_status == 'posted' -> skip entirely, no refresh.
      - anything else -> always refresh order_json + rebuild line items;
        pipeline_status only gets overwritten to 'fetched' if it's
        currently '' / 'fetched' / 'ready'.

    Returns 'created' | 'updated' | 'skipped_posted'.
    """
    current_status = get_order_status(conn, uniware_id)
    if current_status == STATUS_SKIP_ON_REFETCH:
        return "skipped_posted"

    scalar_fields = extract_order_scalar_fields(order)
    lines = build_line_items(order)
    channel_name = extract_channel(order)
    order_date = extract_order_date(order)
    order_json = json.dumps(order, default=str)

    set_status_to_fetched = current_status is None or current_status in STATUS_OVERWRITABLE_ON_REFETCH

    params = {
        "uniware_id": uniware_id,
        "channel_name": channel_name,
        "order_date": order_date,
        "order_json": order_json,
        **scalar_fields,
        "shippingpackages": json.dumps(scalar_fields.get("shippingpackages")) if scalar_fields.get("shippingpackages") is not None else None,
        "returnitems": json.dumps(scalar_fields.get("returnitems")) if scalar_fields.get("returnitems") is not None else None,
    }
    # Ensure every column referenced in the SQL below has a key even when
    # extract_order_scalar_fields() didn't produce one for this order
    # (e.g. no shipping package/return present -- extract_shipping_package_fields
    # and extract_return_fields both return {} in that case, which is the
    # common case: most orders have no return at all). Defaults must match
    # each column's actual nullability -- a genuinely nullable timestamptz/
    # integer column is fine as NULL, but the schema's `text NOT NULL
    # DEFAULT ''` columns reject an explicit NULL outright (the column
    # DEFAULT only applies when the column is omitted from the INSERT
    # entirely, not when NULL is passed for it) -- confirmed 2026-07-24,
    # this crashed on the very first real order fetched.
    for col in ("created", "updated", "displayorderdatetime", "shipment_date", "pin_code"):
        params.setdefault(col, None)
    for col in (
        "channel_id", "status", "cod", "customer_name", "city", "state", "district",
        "shipment_status", "return_code", "statuscode", "shippingprovider",
        "trackingnumber", "trackingstatus", "providerstatus", "putawaycode",
        "returninvoicecode", "returninvoicedisplaycode", "actioncode", "type",
        "returncompleteddate", "inventoryreceiveddate", "returnfacilitycode",
    ):
        params.setdefault(col, "")
    for col in ("uc_taxable_value", "uc_cgst", "uc_sgst", "uc_igst", "uc_shipping", "uc_total"):
        params.setdefault(col, 0)

    with conn.cursor() as cur:
        if set_status_to_fetched:
            cur.execute(
                """
                INSERT INTO unicommerce_orders (
                  uniware_id, channel_name, pipeline_status, order_date, order_json,
                  channel_id, status, created, updated, displayorderdatetime, cod,
                  customer_name, city, state, district, pin_code,
                  shippingpackages, shipment_status, shipment_date,
                  return_code, statuscode, shippingprovider, trackingnumber, trackingstatus,
                  providerstatus, putawaycode, returninvoicecode, returninvoicedisplaycode,
                  actioncode, type, returncompleteddate, inventoryreceiveddate, returnfacilitycode,
                  returnitems, uc_taxable_value, uc_cgst, uc_sgst, uc_igst, uc_shipping, uc_total
                ) VALUES (
                  %(uniware_id)s, %(channel_name)s, 'fetched', %(order_date)s, %(order_json)s,
                  %(channel_id)s, %(status)s, %(created)s, %(updated)s, %(displayorderdatetime)s, %(cod)s,
                  %(customer_name)s, %(city)s, %(state)s, %(district)s, %(pin_code)s,
                  %(shippingpackages)s, %(shipment_status)s, %(shipment_date)s,
                  %(return_code)s, %(statuscode)s, %(shippingprovider)s, %(trackingnumber)s, %(trackingstatus)s,
                  %(providerstatus)s, %(putawaycode)s, %(returninvoicecode)s, %(returninvoicedisplaycode)s,
                  %(actioncode)s, %(type)s, %(returncompleteddate)s, %(inventoryreceiveddate)s, %(returnfacilitycode)s,
                  %(returnitems)s, %(uc_taxable_value)s, %(uc_cgst)s, %(uc_sgst)s, %(uc_igst)s, %(uc_shipping)s, %(uc_total)s
                )
                ON CONFLICT (uniware_id) DO UPDATE SET
                  channel_name = EXCLUDED.channel_name,
                  pipeline_status = 'fetched',
                  order_date = EXCLUDED.order_date,
                  order_json = EXCLUDED.order_json,
                  channel_id = EXCLUDED.channel_id, status = EXCLUDED.status,
                  created = EXCLUDED.created, updated = EXCLUDED.updated,
                  displayorderdatetime = EXCLUDED.displayorderdatetime, cod = EXCLUDED.cod,
                  customer_name = EXCLUDED.customer_name, city = EXCLUDED.city, state = EXCLUDED.state,
                  district = EXCLUDED.district, pin_code = EXCLUDED.pin_code,
                  shippingpackages = EXCLUDED.shippingpackages, shipment_status = EXCLUDED.shipment_status,
                  shipment_date = EXCLUDED.shipment_date, return_code = EXCLUDED.return_code,
                  statuscode = EXCLUDED.statuscode, shippingprovider = EXCLUDED.shippingprovider,
                  trackingnumber = EXCLUDED.trackingnumber, trackingstatus = EXCLUDED.trackingstatus,
                  providerstatus = EXCLUDED.providerstatus, putawaycode = EXCLUDED.putawaycode,
                  returninvoicecode = EXCLUDED.returninvoicecode,
                  returninvoicedisplaycode = EXCLUDED.returninvoicedisplaycode,
                  actioncode = EXCLUDED.actioncode, type = EXCLUDED.type,
                  returncompleteddate = EXCLUDED.returncompleteddate,
                  inventoryreceiveddate = EXCLUDED.inventoryreceiveddate,
                  returnfacilitycode = EXCLUDED.returnfacilitycode, returnitems = EXCLUDED.returnitems,
                  uc_taxable_value = EXCLUDED.uc_taxable_value, uc_cgst = EXCLUDED.uc_cgst,
                  uc_sgst = EXCLUDED.uc_sgst, uc_igst = EXCLUDED.uc_igst,
                  uc_shipping = EXCLUDED.uc_shipping, uc_total = EXCLUDED.uc_total,
                  updated_at = now()
                """,
                params,
            )
        else:
            # Order is in a non-overwritable stage (posting/batch_hold/verify/
            # failed/dead) -- refresh everything except pipeline_status.
            cur.execute(
                """
                INSERT INTO unicommerce_orders (
                  uniware_id, channel_name, pipeline_status, order_date, order_json,
                  channel_id, status, created, updated, displayorderdatetime, cod,
                  customer_name, city, state, district, pin_code,
                  shippingpackages, shipment_status, shipment_date,
                  return_code, statuscode, shippingprovider, trackingnumber, trackingstatus,
                  providerstatus, putawaycode, returninvoicecode, returninvoicedisplaycode,
                  actioncode, type, returncompleteddate, inventoryreceiveddate, returnfacilitycode,
                  returnitems, uc_taxable_value, uc_cgst, uc_sgst, uc_igst, uc_shipping, uc_total
                ) VALUES (
                  %(uniware_id)s, %(channel_name)s, 'fetched', %(order_date)s, %(order_json)s,
                  %(channel_id)s, %(status)s, %(created)s, %(updated)s, %(displayorderdatetime)s, %(cod)s,
                  %(customer_name)s, %(city)s, %(state)s, %(district)s, %(pin_code)s,
                  %(shippingpackages)s, %(shipment_status)s, %(shipment_date)s,
                  %(return_code)s, %(statuscode)s, %(shippingprovider)s, %(trackingnumber)s, %(trackingstatus)s,
                  %(providerstatus)s, %(putawaycode)s, %(returninvoicecode)s, %(returninvoicedisplaycode)s,
                  %(actioncode)s, %(type)s, %(returncompleteddate)s, %(inventoryreceiveddate)s, %(returnfacilitycode)s,
                  %(returnitems)s, %(uc_taxable_value)s, %(uc_cgst)s, %(uc_sgst)s, %(uc_igst)s, %(uc_shipping)s, %(uc_total)s
                )
                ON CONFLICT (uniware_id) DO UPDATE SET
                  channel_name = EXCLUDED.channel_name,
                  order_date = EXCLUDED.order_date,
                  order_json = EXCLUDED.order_json,
                  channel_id = EXCLUDED.channel_id, status = EXCLUDED.status,
                  created = EXCLUDED.created, updated = EXCLUDED.updated,
                  displayorderdatetime = EXCLUDED.displayorderdatetime, cod = EXCLUDED.cod,
                  customer_name = EXCLUDED.customer_name, city = EXCLUDED.city, state = EXCLUDED.state,
                  district = EXCLUDED.district, pin_code = EXCLUDED.pin_code,
                  shippingpackages = EXCLUDED.shippingpackages, shipment_status = EXCLUDED.shipment_status,
                  shipment_date = EXCLUDED.shipment_date, return_code = EXCLUDED.return_code,
                  statuscode = EXCLUDED.statuscode, shippingprovider = EXCLUDED.shippingprovider,
                  trackingnumber = EXCLUDED.trackingnumber, trackingstatus = EXCLUDED.trackingstatus,
                  providerstatus = EXCLUDED.providerstatus, putawaycode = EXCLUDED.putawaycode,
                  returninvoicecode = EXCLUDED.returninvoicecode,
                  returninvoicedisplaycode = EXCLUDED.returninvoicedisplaycode,
                  actioncode = EXCLUDED.actioncode, type = EXCLUDED.type,
                  returncompleteddate = EXCLUDED.returncompleteddate,
                  inventoryreceiveddate = EXCLUDED.inventoryreceiveddate,
                  returnfacilitycode = EXCLUDED.returnfacilitycode, returnitems = EXCLUDED.returnitems,
                  uc_taxable_value = EXCLUDED.uc_taxable_value, uc_cgst = EXCLUDED.uc_cgst,
                  uc_sgst = EXCLUDED.uc_sgst, uc_igst = EXCLUDED.uc_igst,
                  uc_shipping = EXCLUDED.uc_shipping, uc_total = EXCLUDED.uc_total,
                  updated_at = now()
                """,
                params,
            )

        cur.execute("DELETE FROM unicommerce_order_lines WHERE uniware_id = %s", (uniware_id,))
        for line in lines:
            cur.execute(
                """
                INSERT INTO unicommerce_order_lines (
                  uniware_id, item_id, itemsku, sellerskucode, channelproductid, quantity,
                  total_price, selling_price, created, updated, totalintegratedgst,
                  integratedgstpercentage, sellingpricewithouttaxesanddiscount,
                  shippingcharges, shippingmethodcharges, cashondeliverycharges,
                  cancellationreason, state_gst_tax_percentage, central_gst_tax_percentage,
                  statuscode, discount, shippingchargetaxpercentage, bundleskucode, code,
                  batchcode, vendorbatchnumber, uniware_return_status, uniware_return_items
                ) VALUES (
                  %(uniware_id)s, %(item_id)s, %(itemsku)s, %(sellerskucode)s, %(channelproductid)s, %(quantity)s,
                  %(total_price)s, %(selling_price)s, %(created)s, %(updated)s, %(totalintegratedgst)s,
                  %(integratedgstpercentage)s, %(sellingpricewithouttaxesanddiscount)s,
                  %(shippingcharges)s, %(shippingmethodcharges)s, %(cashondeliverycharges)s,
                  %(cancellationreason)s, %(state_gst_tax_percentage)s, %(central_gst_tax_percentage)s,
                  %(statuscode)s, %(discount)s, %(shippingchargetaxpercentage)s, %(bundleskucode)s, %(code)s,
                  %(batchcode)s, %(vendorbatchnumber)s, %(uniware_return_status)s, %(uniware_return_items)s
                )
                """,
                {
                    "uniware_id": uniware_id,
                    **line,
                    "uniware_return_items": json.dumps(line.get("uniware_return_items")) if line.get("uniware_return_items") else None,
                },
            )
    conn.commit()
    return "created" if current_status is None else "updated"


# ── Watermarks + sync log (identical pattern to amazon_us store.py) ──────

def get_watermark(conn: psycopg.Connection, name: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM unicommerce_watermarks WHERE name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def set_watermark(conn: psycopg.Connection, name: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unicommerce_watermarks (name, value) VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (name, value),
        )
    conn.commit()


def start_sync_log(conn: psycopg.Connection, mode: str, window_from: str | None = None, window_to: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO unicommerce_sync_log (mode, window_from, window_to) VALUES (%s, %s, %s) RETURNING id",
            (mode, window_from, window_to),
        )
        run_id = cur.fetchone()[0]
    conn.commit()
    return run_id


def finish_sync_log(conn: psycopg.Connection, run_id: int, status: str, counts: dict | None = None, error: str | None = None) -> None:
    conn.rollback()  # a DB error earlier in the run must not mask this write
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_sync_log SET
              finished_at = now(), status = %s, counts = %s, error_message = %s
            WHERE id = %s
            """,
            (status, json.dumps(counts or {}, default=str), error[:2000] if error else None, run_id),
        )
    conn.commit()


# ── fetched -> ready promotion ────────────────────────────────────────────
# Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
# promote_fetched_to_ready. Orders are staged as 'fetched'; only the
# invoice-eligible ones (COMPLETE + eligible line items -- decided by
# mappers.order_ready_for_invoice) get promoted to 'ready', which is the
# only status post_ready_orders processes. Without this step the whole
# posting pipeline is a no-op -- every order stays stuck at 'fetched'.

def get_fetched_orders(
    conn: psycopg.Connection, limit: int | None = None, start_date: str | None = None, end_date: str | None = None
) -> list[dict]:
    where = ["pipeline_status = 'fetched'"]
    params: list = []
    if start_date and end_date:
        where.append("order_date BETWEEN %s AND %s")
        params.extend([start_date, end_date])
    elif start_date:
        where.append("order_date >= %s")
        params.append(start_date)
    elif end_date:
        where.append("order_date <= %s")
        params.append(end_date)
    sql = f"""
        SELECT uniware_id, order_json, updated_at, last_checked_live_at
        FROM unicommerce_orders
        WHERE {' AND '.join(where)}
        ORDER BY last_checked_live_at ASC NULLS FIRST, order_date ASC, created_at ASC
    """
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_ready(conn: psycopg.Connection, uniware_id: str) -> bool:
    """Conditional 'fetched' -> 'ready' promotion. False = the row was no
    longer 'fetched' (a concurrent run already moved it), so this caller
    must not double-count it as promoted."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_orders SET pipeline_status = 'ready', last_error = '', updated_at = now() "
            "WHERE uniware_id = %s AND pipeline_status = 'fetched'",
            (uniware_id,),
        )
        moved = cur.rowcount == 1
    conn.commit()
    return moved


def defer_fetched(conn: psycopg.Connection, uniware_id: str, reason: str) -> None:
    """Record why an order wasn't promoted, leaving it at 'fetched' so a
    later run re-checks it once the order completes upstream in UC.

    Deliberately does NOT touch last_checked_live_at -- that column tracks
    when this order was last verified against live Unicommerce, not when
    it was last examined against our own cached order_json. Every
    promote_fetched_to_ready cycle re-examines (and re-defers) every
    reachable row regardless of whether anything changed, so if this
    function bumped the same clock the live-recheck gate reads, a row
    re-examined every cron cycle could never accumulate enough staleness
    to ever trigger that recheck -- confirmed live 2026-08-28: 348 orders
    stuck at 'fetched' with a stale cached status (e.g. "PROCESSING")
    that had already gone COMPLETE in Unicommerce days earlier, because
    the old code gated the recheck on updated_at, which this same defer
    call reset every 30 minutes."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_orders SET last_error = %s, updated_at = now() "
            "WHERE uniware_id = %s AND pipeline_status = 'fetched'",
            (reason[:2000], uniware_id),
        )
    conn.commit()


def mark_live_checked(conn: psycopg.Connection, uniware_id: str) -> None:
    """Record that this order was just re-verified against live
    Unicommerce (get_order_detail), independent of whatever
    defer_fetched/mark_ready do to updated_at. See defer_fetched's
    docstring for why this needs to be a separate column."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_orders SET last_checked_live_at = now() WHERE uniware_id = %s",
            (uniware_id,),
        )
    conn.commit()


# ── Invoice posting pipeline ──────────────────────────────────────────────
# Ported from khanal_tech_integrations/unicommerce/workers/invoice.py's
# store-layer operations (frappe.db.set_value / frappe.db.sql calls).

# The SAP AR Invoice DocDate is the SHIP date (shipment_date, an IST business
# date), not the channel order date -- same rule as Walmart and amazon-us-b2c;
# the order date goes to TaxDate/U_OrderedOn. shipment_date is timestamptz
# holding the true dispatch instant, so convert to IST before taking the
# date (a plain ::date would use the DB session zone and shift late-evening
# IST dispatches by a day). Every posting gate -- cutoff floor, period lock,
# claim -- keys off this same expression so they always agree with the date
# the invoice is actually posted on. NULL = not shipped = not postable.
DOC_DATE_SQL = "(shipment_date AT TIME ZONE 'Asia/Kolkata')::date"
DOC_DATE_SELECT = "to_char((shipment_date AT TIME ZONE 'Asia/Kolkata')::date, 'YYYY-MM-DD') AS doc_date"


def get_ready_orders(
    conn: psycopg.Connection, limit: int, cutoff_date: str, ready_before=None, channel_name: str | None = None
) -> list[dict]:
    """cutoff_date is a hard floor on the invoice date = ship date (see
    DOC_DATE_SQL and unicommerce.constants.POSTING_CUTOFF_DATE) -- required,
    not optional, so no caller can accidentally sweep in a pre-cutoff
    order. Orders with no ship date are never returned."""
    where = [
        "pipeline_status = 'ready'",
        "shipment_date IS NOT NULL",
        f"{DOC_DATE_SQL} >= %s",
        f"NOT EXISTS (SELECT 1 FROM unicommerce_period_locks WHERE period = to_char({DOC_DATE_SQL}, 'YYYY-MM') AND locked)",
    ]
    params: list = [cutoff_date]
    if ready_before is not None:
        where.append("updated_at < %s")
        params.append(ready_before)
    if channel_name:
        where.append("channel_name = %s")
        params.append(channel_name)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT uniware_id, channel_name, order_date, {DOC_DATE_SELECT}, order_json, attempt_count
            FROM unicommerce_orders
            WHERE {' AND '.join(where)}
            ORDER BY shipment_date ASC, created_at ASC
            LIMIT %s
            """,
            params,
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows


def delete_pre_cutoff_ready_orders(conn: psycopg.Connection, cutoff_date: str) -> list[dict]:
    """Permanently deletes 'ready' orders with order_date before
    cutoff_date (unicommerce.constants.POSTING_CUTOFF_DATE) -- these can
    never be picked up by get_ready_orders' own cutoff filter and predate
    the 2026-08-12 legacy-Frappe cutover, so they sit in our table
    forever as permanently-inert local state, never to post. User
    explicitly confirmed a hard delete (2026-08-25) over a status-flip
    archive. Deletes the companion unicommerce_order_lines rows first
    (same cleanup upsert_order's own refresh does before reinserting),
    then the order rows, in one transaction. Returns every deleted
    order's full row -- the caller must persist this externally as the
    only remaining record, since this is irreversible and nothing else
    keeps a copy once it commits. cutoff_date is required, not read from
    settings internally, matching get_ready_orders' own "no caller can
    accidentally sweep in the wrong window" convention."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM unicommerce_orders WHERE pipeline_status = 'ready' AND order_date < %s", (cutoff_date,))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        ids = [r["uniware_id"] for r in rows]
        if ids:
            cur.execute("DELETE FROM unicommerce_order_lines WHERE uniware_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM unicommerce_orders WHERE pipeline_status = 'ready' AND order_date < %s", (cutoff_date,))
    conn.commit()
    return rows


def claim_ready_order(conn: psycopg.Connection, uniware_id: str) -> bool:
    """Atomically move an order from 'ready' to 'posting'. False = another
    worker already claimed it (protects against two concurrent posting
    runs, or a manual retry racing a scheduled run, double-posting the
    same order to SAP -- exactly the class of bug fixed in the Frappe
    source's khanal_tech_integrations PR #1301-#1304) OR the invoice's month
    (ship date, see DOC_DATE_SQL) is locked (see unicommerce_period_locks)
    OR the order has no ship date -- this is the actual safety boundary for
    period locks and for "no ship date, no post", not just
    get_ready_orders' soft filter, so no posting path (scheduled, manual
    retry, force, future) can ever bypass them even if it skips the
    read-side filter."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE unicommerce_orders
            SET pipeline_status = 'posting', posting_started_at = now(), updated_at = now()
            WHERE uniware_id = %s AND pipeline_status = 'ready'
              AND shipment_date IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM unicommerce_period_locks
                WHERE period = to_char({DOC_DATE_SQL}, 'YYYY-MM') AND locked
              )
            """,
            (uniware_id,),
        )
        claimed = cur.rowcount == 1
    conn.commit()
    return claimed


_FLEX_BACKFILL_STATUSES = ("fetched", "ready", "failed", "dead", "batch_hold", "verify", "period_locked")


def backfill_flex_shipment_dates(conn: psycopg.Connection, dry_run: bool = True) -> dict:
    """One-off for the ship-date DocDate rule. AMAZON_FLEX orders staged
    before it have shipment_date NULL (flex_fetch builds a synthetic order
    with no UC shippingPackages); Amazon's own shipment-date sits in
    order_json->>'updated'. Without this, in-flight Flex rows older than
    flex_fetch's 2-day re-fetch lookback would be skipped forever as
    "no ship date". Only touches rows still in a pre-posting state -- never
    a 'posted' row, so no existing invoice/DocEntry linkage is affected --
    and only fills NULLs, never overwrites. Only ISO-formatted `updated`
    values are cast (flex_fetch writes Amazon's ISO shipment-date; a
    non-ISO value, e.g. epoch-millis on AMAZON_FLEX_XFZQ, would make the
    ::timestamptz cast abort the entire UPDATE, so it is excluded up
    front). Returns the matching ids so a dry run (the default) can be
    reviewed before applying."""
    where = """
        channel_name = 'AMAZON_FLEX'
        AND shipment_date IS NULL
        AND pipeline_status = ANY(%s)
        AND order_json->>'updated' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT uniware_id, pipeline_status, order_date, order_json->>'updated' FROM unicommerce_orders WHERE {where} ORDER BY order_date",
            (list(_FLEX_BACKFILL_STATUSES),),
        )
        rows = [
            {"uniware_id": r[0], "pipeline_status": r[1], "order_date": str(r[2]), "amazon_shipment_date": r[3]}
            for r in cur.fetchall()
        ]
        updated = 0
        if not dry_run and rows:
            cur.execute(
                f"UPDATE unicommerce_orders SET shipment_date = (order_json->>'updated')::timestamptz, updated_at = now() WHERE {where}",
                (list(_FLEX_BACKFILL_STATUSES),),
            )
            updated = cur.rowcount
    if not dry_run:
        conn.commit()
    return {"dry_run": dry_run, "matched": len(rows), "updated": updated, "orders": rows}


def unclaim_order(conn: psycopg.Connection, uniware_id: str, to_status: str = "ready") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_orders SET pipeline_status = %s, updated_at = now() WHERE uniware_id = %s",
            (to_status, uniware_id),
        )
    conn.commit()


def mark_posted(conn: psycopg.Connection, uniware_id: str, doc_entry: str, doc_num: str, tax_actual: float, payload_json: dict | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders SET
              pipeline_status = 'posted', sap_ar_invoice_docentry = %s, sap_ar_invoice_docnum = %s,
              ar_invoice_tax_actual = %s, batch_hold_since = NULL, last_error = '',
              payload_json = %s, updated_at = now()
            WHERE uniware_id = %s
            """,
            (doc_entry, doc_num, tax_actual, json.dumps(payload_json, default=str) if payload_json else None, uniware_id),
        )
    conn.commit()


def mark_failed(conn: psycopg.Connection, uniware_id: str, error: str, max_retry_attempts: int) -> str:
    """Returns the resulting status ('failed' or 'dead')."""
    with conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM unicommerce_orders WHERE uniware_id = %s", (uniware_id,))
        row = cur.fetchone()
        attempt = int((row[0] if row else 0) or 0) + 1
        new_status = "dead" if attempt >= max(8, max_retry_attempts) else "failed"
        cur.execute(
            """
            UPDATE unicommerce_orders SET
              pipeline_status = %s, attempt_count = %s, last_error = %s,
              batch_hold_since = NULL, updated_at = now()
            WHERE uniware_id = %s
            """,
            (new_status, attempt, error[:2000], uniware_id),
        )
    conn.commit()
    return new_status


def mark_batch_hold(conn: psycopg.Connection, uniware_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders SET
              pipeline_status = 'batch_hold', last_error = %s, updated_at = now(),
              batch_hold_since = COALESCE(batch_hold_since, now())
            WHERE uniware_id = %s
            """,
            (error[:2000], uniware_id),
        )
    conn.commit()


def recover_stuck_posting(conn: psycopg.Connection, timeout_mins: int) -> int:
    """A handler that dies via hard Lambda timeout never reaches
    mark_posted/mark_failed, leaving the order stuck at 'posting' forever
    -- move it to 'verify' after timeout_mins so the next cycle checks SAP
    directly for whether it actually posted before it died."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders
            SET pipeline_status = 'verify', updated_at = now()
            WHERE pipeline_status = 'posting'
              AND (posting_started_at IS NULL OR posting_started_at < now() - (%s || ' minutes')::interval)
            """,
            (timeout_mins,),
        )
        affected = cur.rowcount
    conn.commit()
    return affected


def recover_failed_orders(conn: psycopg.Connection, limit: int) -> int:
    """Exponential backoff (10 min * 2^(attempt-1), capped at 180 min)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id, attempt_count, updated_at FROM unicommerce_orders
            WHERE pipeline_status = 'failed'
            ORDER BY updated_at ASC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        recovered = 0
        for uniware_id, attempt_count, updated_at in rows:
            attempt = max(int(attempt_count or 1), 1)
            cooldown_mins = min(180, 10 * (2 ** max(attempt - 1, 0)))
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - %s))", (updated_at,))
            elapsed_sec = cur.fetchone()[0]
            if elapsed_sec is None or elapsed_sec < (cooldown_mins * 60):
                continue
            cur.execute(
                "UPDATE unicommerce_orders SET pipeline_status = 'ready', updated_at = now() WHERE uniware_id = %s",
                (uniware_id,),
            )
            recovered += 1
    conn.commit()
    return recovered


def recover_batch_hold_orders(conn: psycopg.Connection, limit: int, max_attempts: int, max_hours: int) -> dict:
    """Flat 30-min cooldown (not exponential -- waiting for stock
    replenishment, not a transient error). Escalates to 'dead' at either
    max_attempts or max_hours since batch_hold_since, whichever trips first."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id, attempt_count, batch_hold_since, updated_at, last_error
            FROM unicommerce_orders WHERE pipeline_status = 'batch_hold'
            ORDER BY updated_at ASC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        recovered = escalated = 0
        for uniware_id, attempt_count, batch_hold_since, updated_at, last_error in rows:
            attempt = int(attempt_count or 0)
            hold_since = batch_hold_since or updated_at
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - %s)) / 3600.0", (hold_since,))
            hours_held = float(cur.fetchone()[0] or 0)

            if attempt >= max_attempts or hours_held >= max_hours:
                reason = (
                    f"Batch hold exceeded cap (attempts={attempt}/{max_attempts}, hours={hours_held:.1f}/{max_hours}). "
                    f"Last shortage: {(last_error or 'n/a')[:700]}"
                )
                cur.execute(
                    "UPDATE unicommerce_orders SET pipeline_status = 'dead', last_error = %s, updated_at = now() WHERE uniware_id = %s",
                    (reason[:2000], uniware_id),
                )
                escalated += 1
                continue

            is_reservation_conflict = "reservation_conflict" in str(last_error or "").lower()
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - %s))", (updated_at,))
            elapsed_sec = cur.fetchone()[0]
            if not is_reservation_conflict and (elapsed_sec is None or elapsed_sec < 1800):
                continue
            cur.execute(
                "UPDATE unicommerce_orders SET pipeline_status = 'ready', attempt_count = attempt_count + 1, updated_at = now() WHERE uniware_id = %s",
                (uniware_id,),
            )
            recovered += 1
    conn.commit()
    return {"recovered": recovered, "escalated": escalated}


def reset_dead_orders_for_retry(conn: psycopg.Connection, uniware_ids: list[str]) -> int:
    """Manual unstick for orders parked at 'dead' (recover_batch_hold_orders'
    48h/10-attempt cap) or still sitting at 'batch_hold', once the
    underlying stock shortage has cleared -- e.g. new inventory just
    landed at the warehouse and these specific orders shouldn't wait out
    recover_batch_hold_orders' 30-min cooldown for its next cron pass.
    Clears batch_hold_since and attempt_count too, not just
    pipeline_status -- mark_batch_hold's
    `batch_hold_since = COALESCE(batch_hold_since, now())` never gets
    cleared on the dead-escalation path, so without this a revived order
    that hits batch_hold again would look like it's already been held
    for the old (already-expired) window and get re-escalated on the
    very next recovery pass instead of getting a fresh 48h budget.
    Scoped to explicit ids AND to currently-'dead'/'batch_hold' rows, so
    it can't accidentally revive orders parked dead for an unrelated
    reason (e.g. SAP price-floor rejections) just because their id was
    passed by mistake."""
    if not uniware_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders
            SET pipeline_status = 'ready', attempt_count = 0, batch_hold_since = NULL,
                last_error = '', updated_at = now()
            WHERE pipeline_status IN ('dead', 'batch_hold') AND uniware_id = ANY(%s)
            """,
            (uniware_ids,),
        )
        affected = cur.rowcount
    conn.commit()
    return affected


def reset_posted_order_for_repost(conn: psycopg.Connection, uniware_id: str, expected_doc_entry: str | int) -> bool:
    """One-off correction path: after the caller has confirmed and
    cancelled the given order's live SAP invoice (sap_client.cancel_invoice),
    clears every posted-state field and moves it back to 'ready' so the
    normal posting pipeline picks it up fresh and reposts through current
    logic (e.g. the 2026-09-01 Dogsee BOGO-discount fix). Scoped to an
    exact (uniware_id, current sap_ar_invoice_docentry) match, same
    belt-and-suspenders pattern as reset_dead_orders_for_retry's status
    scoping -- refuses to touch the row if its stored DocEntry has since
    changed from what the caller expected (state moved under us, or wrong
    id passed by mistake). Returns False, no-op, if it doesn't match.
    Caller is responsible for the SAP-side cancel actually having
    succeeded before calling this -- this only touches our own table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders
            SET pipeline_status = 'ready', sap_ar_invoice_docentry = NULL, sap_ar_invoice_docnum = NULL,
                ar_invoice_tax_actual = NULL, ar_invoice_tax_expected = NULL, ar_invoice_tax_diff = NULL,
                ar_invoice_unit_price_expected = NULL, ar_invoice_unit_price_actual = NULL, ar_invoice_unit_price_diff = NULL,
                ar_invoice_reconciled = 0, ar_invoice_reconciliation_status = '', ar_invoice_mismatch_reason = '',
                payload_json = NULL, posting_started_at = NULL, batch_hold_since = NULL,
                attempt_count = 0, last_error = '', updated_at = now()
            WHERE uniware_id = %s AND pipeline_status = 'posted' AND sap_ar_invoice_docentry = %s
            """,
            (uniware_id, str(expected_doc_entry)),
        )
        affected = cur.rowcount == 1
    conn.commit()
    return affected


def get_verify_orders(conn: psycopg.Connection, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT uniware_id, attempt_count FROM unicommerce_orders WHERE pipeline_status = 'verify' ORDER BY updated_at ASC LIMIT %s",
            (limit,),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def requeue_or_dead_from_verify(conn: psycopg.Connection, uniware_id: str, max_retries: int, error_msg: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM unicommerce_orders WHERE uniware_id = %s", (uniware_id,))
        row = cur.fetchone()
        attempt = int((row[0] if row else 0) or 0) + 1
        if attempt >= max_retries:
            cur.execute(
                "UPDATE unicommerce_orders SET pipeline_status = 'dead', attempt_count = %s, last_error = %s, updated_at = now() WHERE uniware_id = %s",
                (attempt, error_msg[:2000], uniware_id),
            )
            conn.commit()
            return "dead"
        cur.execute(
            "UPDATE unicommerce_orders SET pipeline_status = 'ready', attempt_count = %s, last_error = %s, updated_at = now() WHERE uniware_id = %s",
            (attempt, error_msg[:2000], uniware_id),
        )
    conn.commit()
    return "ready"


# ── Return staging (fetch_returns) ────────────────────────────────────────
# Ported from khanal_tech_integrations/unicommerce/workers/fetch.py's
# fetch_returns store-layer operations.

def find_order_ref_by_line_codes(conn: psycopg.Connection, codes: list[str]) -> str | None:
    """Derive a parent order's uniware_id from its staged line-item codes,
    when the return payload didn't carry the sale-order code directly.
    Prefers a posted parent when several match (mirrors the Frappe
    _extract_return_ids fallback against Order Line Items)."""
    if not codes:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.uniware_id, o.pipeline_status
            FROM unicommerce_order_lines l
            JOIN unicommerce_orders o ON o.uniware_id = l.uniware_id
            WHERE l.code = ANY(%s)
            """,
            (list(codes),),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    refs = {r[0] for r in rows}
    if len(refs) == 1:
        return next(iter(refs))
    for uniware_id, status in rows:
        if status == "posted":
            return uniware_id
    return sorted(refs)[0]


def get_cn_status(conn: psycopg.Connection, uc_return_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM unicommerce_cn_staging WHERE uc_return_id = %s", (uc_return_id,))
        row = cur.fetchone()
    return row[0] if row else None


def upsert_cn_staging(
    conn: psycopg.Connection,
    uc_return_id: str,
    uc_order_id: str,
    parent_sap_doc_entry: str,
    return_date: str,
    return_type: str,
    items: list[dict],
    status: str,
    last_error: str,
) -> str:
    """Insert or refresh a UC return's credit-note staging row. Returns
    'created' | 'updated' | 'skipped_posted'. A row already 'posted' is
    never touched; a 'dead' row keeps 'dead' (a re-fetch must not revive
    a terminal credit note) -- same rules as the Frappe fetch_returns."""
    current = get_cn_status(conn, uc_return_id)
    if current == "posted":
        return "skipped_posted"
    effective_status = "dead" if current == "dead" else status
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unicommerce_cn_staging (
              uc_return_id, uc_order_id, parent_sap_doc_entry, return_date,
              return_type, items_json, status, last_error
            ) VALUES (
              %(uc_return_id)s, %(uc_order_id)s, %(parent_sap_doc_entry)s, %(return_date)s,
              %(return_type)s, %(items_json)s, %(status)s, %(last_error)s
            )
            ON CONFLICT (uc_return_id) DO UPDATE SET
              uc_order_id = EXCLUDED.uc_order_id,
              parent_sap_doc_entry = EXCLUDED.parent_sap_doc_entry,
              return_date = EXCLUDED.return_date,
              return_type = EXCLUDED.return_type,
              items_json = EXCLUDED.items_json,
              status = EXCLUDED.status,
              last_error = EXCLUDED.last_error,
              updated_at = now()
            """,
            {
                "uc_return_id": uc_return_id,
                "uc_order_id": uc_order_id,
                "parent_sap_doc_entry": parent_sap_doc_entry,
                "return_date": return_date or None,
                "return_type": return_type,
                "items_json": json.dumps(items, default=str),
                "status": effective_status,
                "last_error": last_error[:2000],
            },
        )
    conn.commit()
    return "updated" if current is not None else "created"


# ── Credit note posting pipeline ──────────────────────────────────────────
# Ported from khanal_tech_integrations/unicommerce/workers/credit_note.py's
# store-layer operations.

def get_order(conn: psycopg.Connection, uniware_id: str) -> dict | None:
    """Parent order lookup for credit-note prep (channel, ship state, raw
    UC status, pipeline status, posted invoice DocEntry + its payload)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id, channel_name, state, status, pipeline_status,
                   sap_ar_invoice_docentry, payload_json
            FROM unicommerce_orders WHERE uniware_id = %s
            """,
            (uniware_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


def get_pending_credit_notes(
    conn: psycopg.Connection,
    limit: int,
    cutoff_date: str,
    start_date: str | None = None,
    end_date: str | None = None,
    channel_name: str | None = None,
) -> list[dict]:
    """Pending/failed CN staging rows, optionally restricted to CNs whose
    **parent order's** order_date falls in [start_date, end_date] and/or
    whose parent order is on channel_name -- matches the Ops Center
    "Post Credit Notes" date-range UX, which keys off the UC sale order
    date (not the return date). cutoff_date is a hard floor on top of
    that -- required, not optional (see unicommerce.constants.
    POSTING_CUTOFF_DATE) -- so a CN whose parent predates the cutoff
    never posts even if start_date isn't passed. A CN whose parent order
    isn't in unicommerce_orders at all (not fetched yet) has NULL
    o.order_date, so this excludes it too until the parent exists."""
    where = [
        "cn.status IN ('pending', 'failed')",
        "o.order_date >= %s",
        "NOT EXISTS (SELECT 1 FROM unicommerce_period_locks WHERE period = to_char(cn.return_date, 'YYYY-MM') AND locked)",
    ]
    params: list = [cutoff_date]
    if start_date or end_date or channel_name:
        where.append("o.uniware_id IS NOT NULL")
        if start_date and end_date:
            where.append("o.order_date BETWEEN %s AND %s")
            params.extend([start_date, end_date])
        elif start_date:
            where.append("o.order_date >= %s")
            params.append(start_date)
        elif end_date:
            where.append("o.order_date <= %s")
            params.append(end_date)
        if channel_name:
            where.append("o.channel_name = %s")
            params.append(channel_name)
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT cn.uc_return_id, cn.uc_order_id, cn.attempt_count, cn.sap_cn_docentry
            FROM unicommerce_cn_staging cn
            LEFT JOIN unicommerce_orders o ON o.uniware_id = cn.uc_order_id
            WHERE {' AND '.join(where)}
            ORDER BY cn.return_date ASC, cn.created_at ASC
            LIMIT %s
            """,
            params,
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_cn(conn: psycopg.Connection, uc_return_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uc_return_id, uc_order_id, parent_sap_doc_entry, return_date,
                   items_json, status, sap_cn_docentry, attempt_count
            FROM unicommerce_cn_staging WHERE uc_return_id = %s
            """,
            (uc_return_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
    result = dict(zip(cols, row))
    # items_json is a jsonb column -- psycopg returns it already parsed
    # (list), not a JSON string, so only json.loads a str.
    raw_items = result.pop("items_json")
    result["items"] = raw_items if isinstance(raw_items, list) else (json.loads(raw_items) if raw_items else [])
    return result


def claim_credit_note(conn: psycopg.Connection, uc_return_id: str) -> bool:
    """Atomically move a CN staging row from pending/failed to 'posting' --
    same double-submission guard as claim_ready_order, for a scheduled run
    racing a manual retry. Not present in the Frappe source (its CN worker
    does a plain frappe.db.set_value with no WHERE-status guard); added
    here since we're writing this store layer fresh and the invoice
    pipeline already established the atomic-claim pattern for exactly
    this race shape. Also the real safety boundary for period locks (see
    claim_ready_order) -- keyed on return_date, which is what actually
    becomes the CN's SAP DocDate (sap_payload.credit_note_payload), not
    the parent order's order_date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_cn_staging
            SET status = 'posting', updated_at = now()
            WHERE uc_return_id = %s AND status IN ('pending', 'failed')
              AND NOT EXISTS (
                SELECT 1 FROM unicommerce_period_locks
                WHERE period = to_char(unicommerce_cn_staging.return_date, 'YYYY-MM') AND locked
              )
            """,
            (uc_return_id,),
        )
        claimed = cur.rowcount == 1
    conn.commit()
    return claimed


def set_cn_prepared(
    conn: psycopg.Connection,
    uc_return_id: str,
    status: str,
    uc_order_id: str | None = None,
    parent_sap_doc_entry: str | None = None,
    items: list[dict] | None = None,
    last_error: str = "",
) -> None:
    """Write the outcome of _prepare_credit_note: status transition plus
    whatever got resolved this pass (uc_order_id once the parent's real
    name is known, parent_sap_doc_entry, and items_json with sapBaseLine/
    sapBatchNumbers filled in so a retry doesn't redo the resolution)."""
    sets = ["status = %s", "updated_at = now()"]
    params: list = [status]
    if uc_order_id is not None:
        sets.append("uc_order_id = %s")
        params.append(uc_order_id)
    if parent_sap_doc_entry is not None:
        sets.append("parent_sap_doc_entry = %s")
        params.append(parent_sap_doc_entry)
    if items is not None:
        sets.append("items_json = %s")
        params.append(json.dumps(items, default=str))
    sets.append("last_error = %s")
    params.append(last_error[:2000])
    params.append(uc_return_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE unicommerce_cn_staging SET {', '.join(sets)} WHERE uc_return_id = %s",
            params,
        )
    conn.commit()


def mark_cn_posted(conn: psycopg.Connection, uc_return_id: str, doc_entry: str, doc_num: str, uc_order_id: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_cn_staging SET
              status = 'posted', sap_cn_docentry = %s, sap_cn_docnum = %s, last_error = '', updated_at = now()
            WHERE uc_return_id = %s
            """,
            (doc_entry, doc_num, uc_return_id),
        )
        if uc_order_id:
            cur.execute(
                "UPDATE unicommerce_orders SET sap_ar_creditnote_docentry = %s, updated_at = now() WHERE uniware_id = %s",
                (doc_entry, uc_order_id),
            )
    conn.commit()


def _save_sap_exchange(conn: psycopg.Connection, sql: str, params: tuple, what: str) -> None:
    """Best-effort: keep SAP's reply for the dashboard. It runs AFTER the document is recorded as posted and
    swallows every error (the columns come from scripts/add_sap_exchange_columns.sql, which may not have been
    applied yet) -- losing a debugging copy must never turn a successful SAP posting into a failure."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        logger.warning("could not save SAP reply for %s", what, exc_info=True)


def save_order_sap_response(conn: psycopg.Connection, uniware_id: str, response: dict | None) -> None:
    if response:
        _save_sap_exchange(
            conn,
            "UPDATE unicommerce_orders SET sap_response_json = %s WHERE uniware_id = %s",
            (json.dumps(response, default=str), uniware_id),
            f"order {uniware_id}",
        )


def save_cn_sap_exchange(conn: psycopg.Connection, uc_return_id: str, payload: dict | None, response: dict | None) -> None:
    if payload or response:
        _save_sap_exchange(
            conn,
            "UPDATE unicommerce_cn_staging SET payload_json = %s, sap_response_json = %s WHERE uc_return_id = %s",
            (json.dumps(payload, default=str) if payload else None, json.dumps(response, default=str) if response else None, uc_return_id),
            f"credit note {uc_return_id}",
        )


def record_attempt(
    conn: psycopg.Connection, doc_type: str, doc_id: str, outcome: str, *,
    request: dict | None = None, response: dict | None = None, error: str | None = None, sap_doc_entry: str | None = None,
) -> None:
    """Append one posting attempt to the history (scripts/create_posting_attempts_table.sql). Best-effort for the
    same reason as _save_sap_exchange: it must never turn a posting outcome into a crash."""
    _save_sap_exchange(
        conn,
        "INSERT INTO unicommerce_posting_attempts (doc_type, doc_id, outcome, sap_doc_entry, request_json, response_json, error) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (doc_type, doc_id, outcome, sap_doc_entry,
         json.dumps(request, default=str) if request else None, json.dumps(response, default=str) if response else None, error),
        f"attempt {doc_type} {doc_id}",
    )


# ── Flex refund credit notes ──────────────────────────────────────────────
# unicommerce_flex_cn_log must exist before flex_refunds.post_flex_refunds
# runs -- this app's DB role is deliberately least-privilege (RDS
# hardening, 2026-08-18) with no CREATE TABLE grant, so schema changes
# here are a one-off manual DDL step against each database (dev + prod),
# same as every other table in this service. See
# scripts/create_flex_cn_log_table.sql for the exact DDL.
# unicommerce_flex_cn_log: separate staging table from unicommerce_cn_staging
# (real UC returns, batch-tracked inventory reversal) -- Amazon Finances API
# refund events aren't UC returns at all (money-only, no physical stock
# movement), so they get their own log rather than force-fitting the
# UC-return shape. Ported from amazon-us-b2c's store.get_cn_log/claim_cn/
# upsert_cn_log, refund_id ("{amazon_order_id}-{PostedDate}") in place of
# their order-id-only key.

def get_flex_cn_log(conn: psycopg.Connection, refund_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT refund_id, amazon_order_id, posted_date, status, doc_entry, doc_num, "
            "base_doc_entry, attempt_count, error, skip_reason "
            "FROM unicommerce_flex_cn_log WHERE refund_id = %s",
            (refund_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


def claim_flex_cn(conn: psycopg.Connection, refund_id: str, amazon_order_id: str, posted_date: str) -> bool:
    """Atomic claim: insert-if-absent, or reclaim a non-'posted' row --
    same double-submission guard as claim_credit_note. Reclaimable after
    30min so a worker crash mid-flight doesn't wedge the row forever.

    Period-lock check is a plain pre-check here (not embedded in the
    INSERT ... ON CONFLICT below -- there's no INSERT-side WHERE to hang
    it on) rather than a race-safe UPDATE guard like claim_ready_order/
    claim_credit_note. Acceptable: locking a month is a rare, deliberate,
    human-triggered action, not a hot path this needs to race against."""
    if is_period_locked(conn, posted_date):
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unicommerce_flex_cn_log (refund_id, amazon_order_id, posted_date, status)
            VALUES (%s, %s, %s, 'posting')
            ON CONFLICT (refund_id) DO UPDATE SET
              status = 'posting', updated_at = now()
            WHERE unicommerce_flex_cn_log.status NOT IN ('posted', 'posting', 'failed_permanent')
               OR unicommerce_flex_cn_log.updated_at < now() - interval '30 minutes'
            """,
            (refund_id, amazon_order_id, posted_date),
        )
        claimed = cur.rowcount == 1
    conn.commit()
    return claimed


def upsert_flex_cn_log(
    conn: psycopg.Connection,
    refund_id: str,
    amazon_order_id: str,
    posted_date: str,
    status: str,
    doc_entry: str | None = None,
    doc_num: str | None = None,
    base_doc_entry: str | None = None,
    payload: dict | None = None,
    response: dict | None = None,
    error: str | None = None,
    skip_reason: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unicommerce_flex_cn_log
              (refund_id, amazon_order_id, posted_date, status, doc_entry, doc_num,
               base_doc_entry, payload_json, response_json, error, skip_reason, attempt_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
            ON CONFLICT (refund_id) DO UPDATE SET
              status = EXCLUDED.status,
              doc_entry = COALESCE(EXCLUDED.doc_entry, unicommerce_flex_cn_log.doc_entry),
              doc_num = COALESCE(EXCLUDED.doc_num, unicommerce_flex_cn_log.doc_num),
              base_doc_entry = COALESCE(EXCLUDED.base_doc_entry, unicommerce_flex_cn_log.base_doc_entry),
              payload_json = COALESCE(EXCLUDED.payload_json, unicommerce_flex_cn_log.payload_json),
              response_json = COALESCE(EXCLUDED.response_json, unicommerce_flex_cn_log.response_json),
              error = EXCLUDED.error,
              skip_reason = EXCLUDED.skip_reason,
              attempt_count = CASE WHEN EXCLUDED.status IN ('failed', 'failed_permanent')
                                    THEN unicommerce_flex_cn_log.attempt_count + 1
                                    ELSE unicommerce_flex_cn_log.attempt_count END,
              updated_at = now()
            """,
            (
                refund_id, amazon_order_id, posted_date, status, doc_entry, doc_num, base_doc_entry,
                json.dumps(payload, default=str) if payload is not None else None,
                json.dumps(response, default=str) if response is not None else None,
                (error or "")[:2000] or None, (skip_reason or "")[:500] or None,
            ),
        )
    conn.commit()


def get_posted_flex_orders_by_amazon_order_id(conn: psycopg.Connection, amazon_order_id: str) -> list[dict]:
    """All posted AMAZON_FLEX invoices for one Amazon order -- usually one
    row, but an order can split across a PRCJ shipment and a real-FBA
    shipment, or (rarely) two PRCJ shipments, each staged as its own
    uniware_id ("FLEX-{order_id}-{shipment_id}", see flex_fetch.py). A
    refund event only carries amazon_order_id, not shipment_id, so the
    caller must disambiguate by which shipment's invoice actually
    contains the refunded SKU."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT uniware_id, order_json, payload_json, sap_ar_invoice_docentry "
            "FROM unicommerce_orders "
            "WHERE channel_name = 'AMAZON_FLEX' AND pipeline_status = 'posted' "
            "AND uniware_id LIKE %s",
            (f"FLEX-{amazon_order_id}-%",),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def mark_cn_failed(conn: psycopg.Connection, uc_return_id: str, error: str, max_retry_attempts: int) -> str:
    """Returns the resulting status ('failed' or 'dead'). Credit notes get
    a higher retry floor than the shared setting (8, same as the Frappe
    source's _mark_failed) -- they fail for transient SAP reasons (base
    doc not finalised, series exhaustion, session timeout), not permanent
    ones, so they shouldn't die before the invoice-posting side of a
    stuck parent has had several cycles to catch up."""
    with conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM unicommerce_cn_staging WHERE uc_return_id = %s", (uc_return_id,))
        row = cur.fetchone()
        attempt = int((row[0] if row else 0) or 0) + 1
        new_status = "dead" if attempt >= max(8, max_retry_attempts) else "failed"
        cur.execute(
            """
            UPDATE unicommerce_cn_staging SET
              status = %s, attempt_count = %s, last_error = %s, updated_at = now()
            WHERE uc_return_id = %s
            """,
            (new_status, attempt, error[:2000], uc_return_id),
        )
    conn.commit()
    return new_status


def set_cn_last_error(conn: psycopg.Connection, uc_return_id: str, note: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_cn_staging SET last_error = %s, updated_at = now() WHERE uc_return_id = %s",
            (note[:2000], uc_return_id),
        )
    conn.commit()


def update_cn_reconciliation(conn: psycopg.Connection, uc_order_id: str, unit_actual: float, tax_actual: float) -> None:
    """CN DocumentLines carry no UnitPrice (SAP fills it from the base
    invoice), so expected == actual by construction here -- what SAP
    computed is the definitive value, same reasoning as the Frappe
    source's post_credit_note reconciliation write."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders SET
              ar_creditnote_unit_price_expected = %s, ar_creditnote_unit_price_actual = %s, ar_creditnote_unit_price_diff = 0,
              ar_creditnote_tax_expected = %s, ar_creditnote_tax_actual = %s, ar_creditnote_tax_diff = 0,
              ar_creditnote_reconciled = 1, ar_creditnote_reconciliation_status = 'Reconciled and matched with SAP',
              updated_at = now()
            WHERE uniware_id = %s
            """,
            (round(unit_actual, 2), round(unit_actual, 2), round(tax_actual, 2), round(tax_actual, 2), uc_order_id),
        )
    conn.commit()


def update_reconciliation_diffs(
    conn: psycopg.Connection, uniware_id: str, tax_expected: float, actual_tax: float,
    unit_diff: float, expected_unit: float, actual_unit: float, mismatch_reason: str = "",
) -> None:
    is_matched = (abs(tax_expected - actual_tax) <= 1.0) and (unit_diff <= 1.0)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders SET
              ar_invoice_tax_actual = %s, ar_invoice_tax_expected = %s, ar_invoice_tax_diff = %s,
              ar_invoice_unit_price_expected = %s, ar_invoice_unit_price_actual = %s, ar_invoice_unit_price_diff = %s,
              ar_invoice_reconciled = %s, ar_invoice_reconciliation_status = %s,
              ar_invoice_mismatch_reason = %s
            WHERE uniware_id = %s
            """,
            (
                actual_tax, round(tax_expected, 2), round(abs(tax_expected - actual_tax), 2),
                round(expected_unit, 2), round(actual_unit, 2), round(unit_diff, 2),
                int(is_matched), "Reconciled and matched with SAP" if is_matched else "Mismatch (SAP vs expected)",
                "" if is_matched else mismatch_reason[:2000],
                uniware_id,
            ),
        )
    conn.commit()


# ── PoGrn: SAP inbound transfer -> UC Purchase Order + GRN ────────────────
# States: pending -> processing (transient claim) -> po_created (committed
# right after PO creation succeeds, before any GRN round starts) -> grn_done.
# A single "processing" sentinel covers both the PO and GRN steps -- which
# sub-step still needs doing is read from uc_po_id, not a separate status
# value per step. This means claim_pogrn's WHERE (pending, po_created) is
# always a safe re-entry point: a crash mid-processing never leaves a row
# unable to resume, as long as mark_pogrn_po_created's commit happens
# before any GRN round is attempted -- there's no UC sandbox to safely
# absorb a duplicate PO from a naive retry.

def stage_pogrn_transfer(conn: psycopg.Connection, sap_doc_entry: int, from_warehouse: str, to_warehouse: str, line_items: list[dict]) -> bool:
    """First-seen insert (the polling cursor's "evaluated" marker). Returns
    True if this is a new row, False if it already existed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unicommerce_pogrn_staging (sap_doc_entry, from_warehouse, to_warehouse, line_items)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (sap_doc_entry) DO NOTHING
            """,
            (sap_doc_entry, from_warehouse, to_warehouse, json.dumps(line_items, default=str)),
        )
        inserted = cur.rowcount == 1
    conn.commit()
    return inserted


def claim_pogrn(conn: psycopg.Connection, sap_doc_entry: int) -> dict | None:
    """Atomically claim for processing. Returns the claimed row (with
    uc_po_id, so the caller knows whether PO creation already happened),
    or None if another worker already claimed it or it's not in a
    claimable state."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_pogrn_staging
            SET status = 'processing', updated_at = now()
            WHERE sap_doc_entry = %s AND status IN ('pending', 'po_created')
            RETURNING sap_doc_entry, from_warehouse, to_warehouse, line_items, uc_po_id, uc_grn_ids, attempt_count
            """,
            (sap_doc_entry,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    cols = ["sap_doc_entry", "from_warehouse", "to_warehouse", "line_items", "uc_po_id", "uc_grn_ids", "attempt_count"]
    return dict(zip(cols, row))


def get_claimable_pogrn_doc_entries(conn: psycopg.Connection, limit: int, to_warehouses: set[str] | None = None) -> list[int]:
    """to_warehouses, if given, restricts to rows whose staged destination
    warehouse is in that set -- rows outside it are left untouched (not
    claimed, not failed), same as pogrn.ENABLED_TO_WAREHOUSES's temporary
    EC-FG-only gate."""
    with conn.cursor() as cur:
        if to_warehouses:
            cur.execute(
                "SELECT sap_doc_entry FROM unicommerce_pogrn_staging WHERE status IN ('pending', 'po_created') "
                "AND to_warehouse = ANY(%s) ORDER BY sap_doc_entry ASC LIMIT %s",
                (list(to_warehouses), limit),
            )
        else:
            cur.execute(
                "SELECT sap_doc_entry FROM unicommerce_pogrn_staging WHERE status IN ('pending', 'po_created') ORDER BY sap_doc_entry ASC LIMIT %s",
                (limit,),
            )
        return [r[0] for r in cur.fetchall()]


def mark_pogrn_po_created(conn: psycopg.Connection, sap_doc_entry: int, uc_po_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_pogrn_staging SET status = 'po_created', uc_po_id = %s, updated_at = now() WHERE sap_doc_entry = %s",
            (uc_po_id, sap_doc_entry),
        )
    conn.commit()


def append_pogrn_grn_id(conn: psycopg.Connection, sap_doc_entry: int, grn_id: str) -> None:
    """Append one GRN id as its round succeeds -- a crash mid-rounds only
    re-attempts what's not yet recorded here."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_pogrn_staging SET uc_grn_ids = uc_grn_ids || %s::jsonb, updated_at = now() WHERE sap_doc_entry = %s",
            (json.dumps([grn_id]), sap_doc_entry),
        )
    conn.commit()


def mark_pogrn_done(conn: psycopg.Connection, sap_doc_entry: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE unicommerce_pogrn_staging SET status = 'grn_done', last_error = '', updated_at = now() WHERE sap_doc_entry = %s",
            (sap_doc_entry,),
        )
    conn.commit()


def mark_pogrn_failed(conn: psycopg.Connection, sap_doc_entry: int, error: str, max_attempts: int) -> str:
    """Returns the resulting status ('failed' or 'dead')."""
    with conn.cursor() as cur:
        cur.execute("SELECT attempt_count FROM unicommerce_pogrn_staging WHERE sap_doc_entry = %s", (sap_doc_entry,))
        row = cur.fetchone()
        attempt = int((row[0] if row else 0) or 0) + 1
        new_status = "dead" if attempt >= max_attempts else "failed"
        cur.execute(
            "UPDATE unicommerce_pogrn_staging SET status = %s, attempt_count = %s, last_error = %s, updated_at = now() WHERE sap_doc_entry = %s",
            (new_status, attempt, error[:2000], sap_doc_entry),
        )
    conn.commit()
    return new_status


def recover_failed_pogrn(conn: psycopg.Connection, limit: int, max_attempts: int, max_hours: int) -> dict:
    """Flat 30-min cooldown before retry, escalating to 'dead' past the
    cap -- mirrors recover_batch_hold_orders' idiom. A retried row resumes
    at 'po_created' (not 'pending') if a PO already exists, so it never
    re-attempts PO creation."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sap_doc_entry, uc_po_id, attempt_count, created_at, updated_at
            FROM unicommerce_pogrn_staging WHERE status = 'failed'
            ORDER BY updated_at ASC LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        recovered = escalated = 0
        for sap_doc_entry, uc_po_id, attempt_count, created_at, updated_at in rows:
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - %s)) / 3600.0", (created_at,))
            age_hours = float(cur.fetchone()[0] or 0)
            if int(attempt_count or 0) >= max_attempts or age_hours >= max_hours:
                cur.execute("UPDATE unicommerce_pogrn_staging SET status = 'dead', updated_at = now() WHERE sap_doc_entry = %s", (sap_doc_entry,))
                escalated += 1
                continue
            cur.execute("SELECT EXTRACT(EPOCH FROM (now() - %s)) / 60.0", (updated_at,))
            cooldown_elapsed_min = float(cur.fetchone()[0] or 0)
            if cooldown_elapsed_min < 30:
                continue
            resume_status = "po_created" if uc_po_id else "pending"
            cur.execute("UPDATE unicommerce_pogrn_staging SET status = %s, updated_at = now() WHERE sap_doc_entry = %s", (resume_status, sap_doc_entry))
            recovered += 1
    conn.commit()
    return {"recovered": recovered, "escalated": escalated}


def get_pogrn_transfer(conn: psycopg.Connection, sap_doc_entry: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sap_doc_entry, from_warehouse, to_warehouse, line_items, status, uc_po_id, uc_grn_ids, attempt_count, last_error FROM unicommerce_pogrn_staging WHERE sap_doc_entry = %s",
            (sap_doc_entry,),
        )
        row = cur.fetchone()
    if not row:
        return None
    cols = ["sap_doc_entry", "from_warehouse", "to_warehouse", "line_items", "status", "uc_po_id", "uc_grn_ids", "attempt_count", "last_error"]
    return dict(zip(cols, row))


# ── Recon: periodic re-verification + backfill ────────────────────────────
# Read-only against SAP, writes only to our own Postgres -- no claim/state
# machine needed at all (see docs/superpowers/specs/2026-07-27-recon-design.md).

def get_recon_invoice_candidates(
    conn: psycopg.Connection, start_date: str, end_date: str, limit: int,
    min_tax_diff: float = 1.0, min_unit_price_diff: float = 1.0,
) -> list[str]:
    """uniware_ids of posted orders with an invoice, order_date in the
    window, prioritized unreconciled-first then biggest-mismatch-first --
    same idea as the Frappe source's refresh_recon ORDER BY."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id FROM unicommerce_orders
            WHERE pipeline_status = 'posted'
              AND sap_ar_invoice_docentry IS NOT NULL AND sap_ar_invoice_docentry != ''
              AND order_date BETWEEN %s AND %s
              AND (
                ar_invoice_reconciliation_status = ''
                OR COALESCE(ar_invoice_tax_diff, 0) >= %s
                OR COALESCE(ar_invoice_unit_price_diff, 0) >= %s
              )
            ORDER BY
              CASE WHEN ar_invoice_reconciliation_status = '' THEN 0 ELSE 1 END ASC,
              GREATEST(COALESCE(ar_invoice_tax_diff, 0), COALESCE(ar_invoice_unit_price_diff, 0)) DESC,
              updated_at DESC
            LIMIT %s
            """,
            (start_date, end_date, min_tax_diff, min_unit_price_diff, limit),
        )
        return [r[0] for r in cur.fetchall()]


def get_recon_cn_candidates(conn: psycopg.Connection, start_date: str, end_date: str, limit: int) -> list[str]:
    """uniware_ids of orders with a posted CN never yet reconciled. CN
    can't mismatch by construction (SAP Credit Notes are "Copy From" the
    base invoice -- see update_cn_reconciliation's docstring), so this is
    backfill-only, no diff-based re-check."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id FROM unicommerce_orders
            WHERE sap_ar_creditnote_docentry IS NOT NULL AND sap_ar_creditnote_docentry != ''
              AND order_date BETWEEN %s AND %s
              AND ar_creditnote_reconciliation_status = ''
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (start_date, end_date, limit),
        )
        return [r[0] for r in cur.fetchall()]


def get_order_for_recon(conn: psycopg.Connection, uniware_id: str) -> dict | None:
    """Full order snapshot needed to rebuild the invoice payload for
    reconciliation (order_json/channel_name/order_date -- get_order's
    existing shape is scoped to credit-note prep and doesn't carry
    these)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT uniware_id, channel_name, order_date, order_json,
                   sap_ar_invoice_docentry, sap_ar_creditnote_docentry,
                   CASE WHEN shipment_date IS NULL THEN NULL
                        ELSE to_char((shipment_date AT TIME ZONE 'Asia/Kolkata')::date, 'YYYY-MM-DD') END AS doc_date
            FROM unicommerce_orders WHERE uniware_id = %s
            """,
            (uniware_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d.name for d in cur.description]
    return dict(zip(cols, row))


# ── Monthly posting-period lock/unlock ─────────────────────────────────────
# Self-contained mirror of SAP B1's own posting-period lock, controlled
# entirely from our own dashboard/DB (does not read SAP's actual period-lock
# state). The real enforcement lives in claim_ready_order/claim_credit_note/
# claim_flex_cn above -- these functions are the admin surface (lock/unlock/
# list) plus the in-flight-row relabeling sweep for dashboard visibility.

def is_period_locked(conn: psycopg.Connection, date_val) -> bool:
    """date_val: a date/str for the doc date about to be used (ship date
    for invoices, return_date/posted_date for credit notes)."""
    period = str(date_val)[:7]
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM unicommerce_period_locks WHERE period = %s AND locked", (period,))
        return cur.fetchone() is not None


def list_period_locks(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT period, locked, locked_by, locked_at, unlocked_by, unlocked_at, note
            FROM unicommerce_period_locks
            ORDER BY period DESC
            """
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def lock_period(conn: psycopg.Connection, period: str, actor: str, note: str = "") -> dict:
    """Locks `period` ('YYYY-MM') and relabels every currently ready/pending
    order and credit note in that month to 'period_locked' so the dashboard
    shows why they're stuck. This relabel is a UX convenience only -- the
    real gate is claim_ready_order/claim_credit_note/claim_flex_cn checking
    unicommerce_period_locks directly, so a row that stays 'ready' (e.g. a
    fetch landing after this sweep runs) still can't post."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO unicommerce_period_locks (period, locked, locked_by, locked_at, note, updated_at)
            VALUES (%s, true, %s, now(), %s, now())
            ON CONFLICT (period) DO UPDATE SET
              locked = true, locked_by = %s, locked_at = now(), note = %s,
              unlocked_by = NULL, unlocked_at = NULL, updated_at = now()
            """,
            (period, actor, note, actor, note),
        )
        cur.execute(
            "UPDATE unicommerce_orders SET pipeline_status = 'period_locked', updated_at = now() "
            f"WHERE pipeline_status = 'ready' AND shipment_date IS NOT NULL AND to_char({DOC_DATE_SQL}, 'YYYY-MM') = %s",
            (period,),
        )
        orders_held = cur.rowcount
        cur.execute(
            "UPDATE unicommerce_cn_staging SET status = 'period_locked', updated_at = now() "
            "WHERE status IN ('pending', 'failed') AND to_char(return_date, 'YYYY-MM') = %s",
            (period,),
        )
        cns_held = cur.rowcount
    conn.commit()
    return {"period": period, "locked": True, "orders_held": orders_held, "credit_notes_held": cns_held}


def unlock_period(conn: psycopg.Connection, period: str, actor: str) -> dict:
    """Unlocks `period` and flips every 'period_locked' order/CN in that
    month back to ready/pending so the next scheduled run (or a manual
    retry) picks them up with no separate replay step."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_period_locks
            SET locked = false, unlocked_by = %s, unlocked_at = now(), updated_at = now()
            WHERE period = %s
            """,
            (actor, period),
        )
        found = cur.rowcount == 1
        cur.execute(
            "UPDATE unicommerce_orders SET pipeline_status = 'ready', updated_at = now() "
            f"WHERE pipeline_status = 'period_locked' AND shipment_date IS NOT NULL AND to_char({DOC_DATE_SQL}, 'YYYY-MM') = %s",
            (period,),
        )
        orders_resumed = cur.rowcount
        cur.execute(
            "UPDATE unicommerce_cn_staging SET status = 'pending', updated_at = now() "
            "WHERE status = 'period_locked' AND to_char(return_date, 'YYYY-MM') = %s",
            (period,),
        )
        cns_resumed = cur.rowcount
    conn.commit()
    return {"period": period, "locked": False, "found": found, "orders_resumed": orders_resumed, "credit_notes_resumed": cns_resumed}

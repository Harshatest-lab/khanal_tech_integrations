"""Postgres persistence for the Unicommerce B2C pipeline (psycopg, repo style).

Only the fetch worker's needs are implemented so far -- see
docs/migration/03-unicommerce-b2c.md for the full 7-function plan.
"""

from __future__ import annotations

import json

import psycopg

from unicommerce import STATUS_OVERWRITABLE_ON_REFETCH, STATUS_SKIP_ON_REFETCH
from unicommerce.credentials import DATABASE_URL
from unicommerce.mappers import build_line_items, extract_channel, extract_order_scalar_fields
from unicommerce.dates import extract_order_date


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
    # (e.g. no shipping package present yet).
    for col in (
        "channel_id", "status", "created", "updated", "displayorderdatetime", "cod",
        "customer_name", "city", "state", "district", "pin_code",
        "shipment_status", "shipment_date", "return_code", "statuscode",
        "shippingprovider", "trackingnumber", "trackingstatus", "providerstatus",
        "putawaycode", "returninvoicecode", "returninvoicedisplaycode", "actioncode",
        "type", "returncompleteddate", "inventoryreceiveddate", "returnfacilitycode",
        "uc_taxable_value", "uc_cgst", "uc_sgst", "uc_igst", "uc_shipping", "uc_total",
    ):
        params.setdefault(col, None)

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


# ── Invoice posting pipeline ──────────────────────────────────────────────
# Ported from khanal_tech_integrations/unicommerce/workers/invoice.py's
# store-layer operations (frappe.db.set_value / frappe.db.sql calls).

def get_ready_orders(conn: psycopg.Connection, limit: int, ready_before=None, channel_name: str | None = None) -> list[dict]:
    where = ["pipeline_status = 'ready'"]
    params: list = []
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
            SELECT uniware_id, channel_name, order_date, order_json, attempt_count
            FROM unicommerce_orders
            WHERE {' AND '.join(where)}
            ORDER BY order_date ASC, created_at ASC
            LIMIT %s
            """,
            params,
        )
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows


def claim_ready_order(conn: psycopg.Connection, uniware_id: str) -> bool:
    """Atomically move an order from 'ready' to 'posting'. False = another
    worker already claimed it (protects against two concurrent posting
    runs, or a manual retry racing a scheduled run, double-posting the
    same order to SAP -- exactly the class of bug fixed in the Frappe
    source's khanal_tech_integrations PR #1301-#1304)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders
            SET pipeline_status = 'posting', posting_started_at = now(), updated_at = now()
            WHERE uniware_id = %s AND pipeline_status = 'ready'
            """,
            (uniware_id,),
        )
        claimed = cur.rowcount == 1
    conn.commit()
    return claimed


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
    start_date: str | None = None,
    end_date: str | None = None,
    channel_name: str | None = None,
) -> list[dict]:
    """Pending/failed CN staging rows, optionally restricted to CNs whose
    **parent order's** order_date falls in [start_date, end_date] and/or
    whose parent order is on channel_name -- matches the Ops Center
    "Post Credit Notes" date-range UX, which keys off the UC sale order
    date (not the return date)."""
    where = ["cn.status IN ('pending', 'failed')"]
    params: list = []
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
    result["items"] = json.loads(result.pop("items_json") or "[]")
    return result


def claim_credit_note(conn: psycopg.Connection, uc_return_id: str) -> bool:
    """Atomically move a CN staging row from pending/failed to 'posting' --
    same double-submission guard as claim_ready_order, for a scheduled run
    racing a manual retry. Not present in the Frappe source (its CN worker
    does a plain frappe.db.set_value with no WHERE-status guard); added
    here since we're writing this store layer fresh and the invoice
    pipeline already established the atomic-claim pattern for exactly
    this race shape."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_cn_staging
            SET status = 'posting', updated_at = now()
            WHERE uc_return_id = %s AND status IN ('pending', 'failed')
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


def update_reconciliation_diffs(conn: psycopg.Connection, uniware_id: str, tax_expected: float, actual_tax: float, unit_diff: float, expected_unit: float, actual_unit: float) -> None:
    is_matched = (abs(tax_expected - actual_tax) <= 1.0) and (unit_diff <= 1.0)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE unicommerce_orders SET
              ar_invoice_tax_actual = %s, ar_invoice_tax_expected = %s, ar_invoice_tax_diff = %s,
              ar_invoice_unit_price_expected = %s, ar_invoice_unit_price_actual = %s, ar_invoice_unit_price_diff = %s,
              ar_invoice_reconciled = %s, ar_invoice_reconciliation_status = %s
            WHERE uniware_id = %s
            """,
            (
                actual_tax, round(tax_expected, 2), round(abs(tax_expected - actual_tax), 2),
                round(expected_unit, 2), round(actual_unit, 2), round(unit_diff, 2),
                int(is_matched), "Reconciled and matched with SAP" if is_matched else "Mismatch (SAP vs expected)",
                uniware_id,
            ),
        )
    conn.commit()

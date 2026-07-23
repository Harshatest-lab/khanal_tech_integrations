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

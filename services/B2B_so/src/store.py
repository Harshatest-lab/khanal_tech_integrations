"""Postgres instead of Frappe doctypes."""

from __future__ import annotations

import json
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

import permissions

CHANNEL_BLINKIT = "blinkit"
CHANNEL_LOGISTICS = "logistics"
ALL_CHANNELS = ["blinkit", "zepto", "instamart", "bigbasket", "more", CHANNEL_LOGISTICS]
FETCH_CHANNELS = ["blinkit", "zepto", "instamart", "bigbasket"]
STATUS_PARSED = "parsed"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_READY = "ready"
STATUS_APPROVED = "approved"
STATUS_POSTED = "posted"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
try:
    from credentials_local import DATABASE_URL as _LOCAL_URL  # type: ignore

    if _LOCAL_URL:
        DATABASE_URL = _LOCAL_URL.strip()
except ImportError:
    pass


def connect() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(DATABASE_URL, options="-c search_path=b2b,public")
    _ensure_user_channels(conn)
    _ensure_po_columns(conn)
    return conn


def _ensure_user_channels(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE b2b.users
            ADD COLUMN IF NOT EXISTS channels text[] NOT NULL DEFAULT ARRAY['blinkit']::text[]
            """
        )
        cur.execute(
            "ALTER TABLE b2b.users ADD COLUMN IF NOT EXISTS perm_orders text NOT NULL DEFAULT ''"
        )
        cur.execute(
            "ALTER TABLE b2b.users ADD COLUMN IF NOT EXISTS perm_sku_map text NOT NULL DEFAULT ''"
        )
        cur.execute(
            "ALTER TABLE b2b.users ADD COLUMN IF NOT EXISTS perm_ship_to text NOT NULL DEFAULT ''"
        )
        cur.execute(
            "ALTER TABLE b2b.users ADD COLUMN IF NOT EXISTS perm_delivery text NOT NULL DEFAULT ''"
        )
    conn.commit()


def _ensure_po_columns(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE b2b.purchase_orders ADD COLUMN IF NOT EXISTS sap_response jsonb")
        cur.execute("ALTER TABLE b2b.purchase_orders ADD COLUMN IF NOT EXISTS sap_doc_total numeric")
        cur.execute("ALTER TABLE b2b.purchase_orders ADD COLUMN IF NOT EXISTS sap_doc_date date")
        cur.execute(
            "ALTER TABLE b2b.purchase_orders ADD COLUMN IF NOT EXISTS duplicate_records_status text NOT NULL DEFAULT 'False/0'"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS b2b.po_amend_history (
              id                    bigserial PRIMARY KEY,
              channel               text        NOT NULL DEFAULT 'blinkit',
              po_number             text        NOT NULL,
              po_id                 bigint      NOT NULL REFERENCES b2b.purchase_orders(id) ON DELETE CASCADE,
              record_data           jsonb       NOT NULL,
              sap_docentry          integer,
              sap_docnum            integer,
              sap_status            text        NOT NULL DEFAULT '',
              sap_document_status   text        NOT NULL DEFAULT '',
              pipeline_status       text        NOT NULL DEFAULT '',
              email_subject         text        NOT NULL DEFAULT '',
              email_uid             text        NOT NULL DEFAULT '',
              archived_at           timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS po_amend_history_po_idx
              ON b2b.po_amend_history (channel, po_number, archived_at DESC)
            """
        )
    conn.commit()


def _normalize_channels(raw: Any, role: str = "") -> list[str]:
    """Keep assigned channels as selected (including logistics-only). Role is unused."""
    _ = role
    values = [str(c).strip().lower() for c in (raw or []) if str(c).strip()]
    out = [c for c in values if c in ALL_CHANNELS]
    # Empty assignment → blinkit; logistics alone is a valid Delivery-only user.
    return out if out else [CHANNEL_BLINKIT]


def already_processed(conn: psycopg.Connection, channel: str, email_uid: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM b2b.processed_emails WHERE channel = %s AND email_uid = %s",
            (channel, email_uid),
        )
        return cur.fetchone() is not None


def mark_email_processed(conn: psycopg.Connection, channel: str, email_uid: str, po_number: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO b2b.processed_emails (channel, email_uid, po_number)
            VALUES (%s, %s, %s)
            ON CONFLICT (channel, email_uid) DO UPDATE SET po_number = EXCLUDED.po_number
            """,
            (channel, email_uid, po_number or ""),
        )


def upsert_purchase_order(
    conn: psycopg.Connection,
    header: dict[str, Any],
    lines: list[dict[str, Any]],
    *,
    force_update: bool = False,
) -> dict[str, Any]:
    channel = header.get("channel") or CHANNEL_BLINKIT
    po_number = header["po_number"]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, pipeline_status FROM b2b.purchase_orders WHERE channel = %s AND po_number = %s",
            (channel, po_number),
        )
        existing = cur.fetchone()
        if existing and existing["pipeline_status"] == STATUS_POSTED and not force_update:
            return dict(existing) | {"skipped": True, "reason": "already_posted"}

        params = {
            "channel": channel,
            "po_number": po_number,
            "pipeline_status": header.get("pipeline_status") or STATUS_PARSED,
            "company_name": header.get("company_name") or "",
            "contact_name": header.get("contact_name") or "",
            "po_expiry_date": header.get("po_expiry_date"),
            "delivered_to": header.get("delivered_to") or "",
            "gst_no": header.get("gst_no") or "",
            "customer_code": header.get("customer_code") or "",
            "salesperson_code": header.get("salesperson_code") or "",
            "matched_address_name": header.get("matched_address_name") or "",
            "matched_state": header.get("matched_state") or "",
            "billing_from": header.get("billing_from") or "KT",
            "bill_to": header.get("bill_to") or "",
            "email_uid": header.get("email_uid") or "",
            "email_subject": header.get("email_subject") or "",
            "pdf_s3_key": header.get("pdf_s3_key") or "",
            "xlsx_s3_key": header.get("xlsx_s3_key") or "",
            "total_quantity": header.get("total_quantity") or 0,
            "total_amount": header.get("total_amount") or 0,
            "total_items": header.get("total_items") or len(lines),
            "last_error": header.get("last_error") or "",
            "duplicate_records_status": header.get("duplicate_records_status") or "False/0",
        }
        conflict_where = "" if force_update else "WHERE b2b.purchase_orders.pipeline_status <> 'posted'"
        sap_clear = """
              sap_status = '',
              sap_docentry = NULL,
              sap_docnum = NULL,
              sap_document_status = '',
              sap_response = NULL,
              sap_doc_total = NULL,
              sap_doc_date = NULL,""" if force_update else ""
        cur.execute(
            f"""
            INSERT INTO b2b.purchase_orders (
              channel, po_number, pipeline_status, company_name, contact_name, po_expiry_date,
              delivered_to, gst_no, customer_code, salesperson_code, matched_address_name,
              matched_state, billing_from, bill_to, email_uid, email_subject, pdf_s3_key, xlsx_s3_key,
              total_quantity, total_amount, total_items, last_error, duplicate_records_status, updated_at
            ) VALUES (
              %(channel)s, %(po_number)s, %(pipeline_status)s, %(company_name)s, %(contact_name)s,
              %(po_expiry_date)s, %(delivered_to)s, %(gst_no)s, %(customer_code)s, %(salesperson_code)s,
              %(matched_address_name)s, %(matched_state)s, %(billing_from)s, %(bill_to)s,
              %(email_uid)s, %(email_subject)s, %(pdf_s3_key)s, %(xlsx_s3_key)s,
              %(total_quantity)s, %(total_amount)s, %(total_items)s, %(last_error)s,
              %(duplicate_records_status)s, now()
            )
            ON CONFLICT (channel, po_number) DO UPDATE SET
              pipeline_status = EXCLUDED.pipeline_status,
              company_name = EXCLUDED.company_name,
              contact_name = EXCLUDED.contact_name,
              po_expiry_date = EXCLUDED.po_expiry_date,
              delivered_to = EXCLUDED.delivered_to,
              gst_no = EXCLUDED.gst_no,
              customer_code = EXCLUDED.customer_code,
              salesperson_code = EXCLUDED.salesperson_code,
              matched_address_name = EXCLUDED.matched_address_name,
              matched_state = EXCLUDED.matched_state,
              billing_from = EXCLUDED.billing_from,
              bill_to = EXCLUDED.bill_to,
              email_uid = EXCLUDED.email_uid,
              email_subject = EXCLUDED.email_subject,
              pdf_s3_key = EXCLUDED.pdf_s3_key,
              xlsx_s3_key = EXCLUDED.xlsx_s3_key,
              total_quantity = EXCLUDED.total_quantity,
              total_amount = EXCLUDED.total_amount,
              total_items = EXCLUDED.total_items,
              last_error = EXCLUDED.last_error,
              duplicate_records_status = EXCLUDED.duplicate_records_status,{sap_clear}
              updated_at = now()
            {conflict_where}
            RETURNING *
            """,
            params,
        )
        row = cur.fetchone()
        if not row:
            if existing:
                return dict(existing) | {"skipped": True, "reason": "update_blocked"}
            raise RuntimeError(f"Could not upsert PO {po_number}")
        po_id = int(row["id"])
        cur.execute("DELETE FROM b2b.po_lines WHERE po_id = %s", (po_id,))
        for i, line in enumerate(lines, start=1):
            cur.execute(
                """
                INSERT INTO b2b.po_lines (
                  po_id, line_no, item_code, sap_code, item_name, quantity, discount,
                  product_description, basic_cost_price, igst_percentage, tax_amount,
                  landing_rate, mrp, total_amount, tax_rate, gst_tax, igst_tax, taxcode
                ) VALUES (
                  %(po_id)s, %(line_no)s, %(item_code)s, %(sap_code)s, %(item_name)s, %(quantity)s,
                  %(discount)s, %(product_description)s, %(basic_cost_price)s, %(igst_percentage)s,
                  %(tax_amount)s, %(landing_rate)s, %(mrp)s, %(total_amount)s, %(tax_rate)s,
                  %(gst_tax)s, %(igst_tax)s, %(taxcode)s
                )
                """,
                {
                    "po_id": po_id,
                    "line_no": i,
                    "item_code": str(line.get("item_code") or ""),
                    "sap_code": line.get("sap_code") or "",
                    "item_name": line.get("item_name") or "",
                    "quantity": line.get("quantity") or 0,
                    "discount": line.get("discount") or 0,
                    "product_description": line.get("product_description") or "",
                    "basic_cost_price": line.get("basic_cost_price") or 0,
                    "igst_percentage": line.get("igst_percentage") or 0,
                    "tax_amount": line.get("tax_amount") or 0,
                    "landing_rate": line.get("landing_rate") or 0,
                    "mrp": line.get("mrp") or 0,
                    "total_amount": line.get("total_amount") or 0,
                    "tax_rate": line.get("tax_rate") or 0,
                    "gst_tax": line.get("gst_tax") or "",
                    "igst_tax": line.get("igst_tax") or "",
                    "taxcode": line.get("taxcode") or "",
                },
            )
    return dict(row)


def list_sku_map(conn: psycopg.Connection, channel: str = CHANNEL_BLINKIT) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT item_code, sap_code FROM b2b.sku_map WHERE channel = %s ORDER BY item_code",
            (channel,),
        )
        return [{"item_code": r[0], "sap_code": r[1]} for r in cur.fetchall()]


def sku_map_dict(conn: psycopg.Connection, channel: str = CHANNEL_BLINKIT) -> dict[str, str]:
    return {row["item_code"]: row["sap_code"] for row in list_sku_map(conn, channel)}


def upsert_sku(conn: psycopg.Connection, item_code: str, sap_code: str, channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO b2b.sku_map (channel, item_code, sap_code, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (channel, item_code) DO UPDATE SET sap_code = EXCLUDED.sap_code, updated_at = now()
            """,
            (channel, item_code.strip(), sap_code.strip()),
        )


def import_sku_map(conn: psycopg.Connection, rows: list[dict[str, Any]], channel: str = CHANNEL_BLINKIT) -> int:
    count = 0
    for row in rows:
        item_code = str(row.get("item_code") or "").strip()
        sap_code = str(row.get("sap_code") or "").strip()
        if not item_code or not sap_code:
            continue
        upsert_sku(conn, item_code, sap_code, channel=channel)
        count += 1
    return count


def list_ship_to_aliases(conn: psycopg.Connection, channel: str = CHANNEL_BLINKIT) -> list[dict[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT address_id, address_entry FROM b2b.ship_to_aliases WHERE channel = %s",
            (channel,),
        )
        return [{"address_id": r[0], "address_entry": r[1]} for r in cur.fetchall()]


def upsert_ship_to_alias(conn: psycopg.Connection, address_id: str, address_entry: str, channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO b2b.ship_to_aliases (channel, address_id, address_entry, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (channel, address_id) DO UPDATE SET address_entry = EXCLUDED.address_entry, updated_at = now()
            """,
            (channel, address_id.strip(), address_entry.strip()),
        )


def import_ship_to_aliases(conn: psycopg.Connection, rows: list[dict[str, Any]], channel: str = CHANNEL_BLINKIT) -> int:
    count = 0
    for row in rows:
        address_id = str(row.get("address_id") or "").strip()
        address_entry = str(row.get("address_entry") or "").strip()
        if not address_id or not address_entry:
            continue
        upsert_ship_to_alias(conn, address_id, address_entry, channel=channel)
        count += 1
    return count


def delete_sku(conn: psycopg.Connection, item_code: str, channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM b2b.sku_map WHERE channel = %s AND item_code = %s", (channel, item_code.strip()))


def delete_ship_to_alias(conn: psycopg.Connection, address_id: str, channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM b2b.ship_to_aliases WHERE channel = %s AND address_id = %s",
            (channel, address_id.strip()),
        )


def update_po(conn: psycopg.Connection, po_number: str, header: dict[str, Any], lines: list[dict[str, Any]], channel: str = CHANNEL_BLINKIT) -> dict[str, Any]:
    current = get_po(conn, po_number, channel)
    if not current:
        raise ValueError("PO not found")
    if current["header"].get("pipeline_status") == STATUS_POSTED:
        raise ValueError("Posted POs cannot be edited")
    qty = sum(float(l.get("quantity") or 0) for l in lines)
    amt = sum(float(l.get("total_amount") or 0) for l in lines)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE b2b.purchase_orders SET
              po_expiry_date = %s,
              delivered_to = %s,
              gst_no = %s,
              matched_address_name = %s,
              bill_to = %s,
              total_quantity = %s,
              total_amount = %s,
              total_items = %s,
              updated_at = now()
            WHERE channel = %s AND po_number = %s
            """,
            (
                header.get("po_expiry_date") or None,
                header.get("delivered_to") or "",
                header.get("gst_no") or "",
                header.get("matched_address_name") or "",
                header.get("bill_to") or "",
                qty,
                amt,
                len({str(l.get("item_code")) for l in lines}),
                channel,
                po_number,
            ),
        )
        po_id = int(current["header"]["id"])
        cur.execute("DELETE FROM b2b.po_lines WHERE po_id = %s", (po_id,))
        for i, line in enumerate(lines, start=1):
            cur.execute(
                """
                INSERT INTO b2b.po_lines (
                  po_id, line_no, item_code, sap_code, item_name, quantity, discount,
                  product_description, basic_cost_price, igst_percentage, tax_amount,
                  landing_rate, mrp, total_amount, tax_rate, gst_tax, igst_tax, taxcode
                ) VALUES (
                  %(po_id)s, %(line_no)s, %(item_code)s, %(sap_code)s, %(item_name)s, %(quantity)s,
                  %(discount)s, %(product_description)s, %(basic_cost_price)s, %(igst_percentage)s,
                  %(tax_amount)s, %(landing_rate)s, %(mrp)s, %(total_amount)s, %(tax_rate)s,
                  %(gst_tax)s, %(igst_tax)s, %(taxcode)s
                )
                """,
                {
                    "po_id": po_id,
                    "line_no": i,
                    "item_code": str(line.get("item_code") or ""),
                    "sap_code": line.get("sap_code") or "",
                    "item_name": line.get("item_name") or "",
                    "quantity": line.get("quantity") or 0,
                    "discount": line.get("discount") or 0,
                    "product_description": line.get("product_description") or "",
                    "basic_cost_price": line.get("basic_cost_price") or 0,
                    "igst_percentage": line.get("igst_percentage") or 0,
                    "tax_amount": line.get("tax_amount") or 0,
                    "landing_rate": line.get("landing_rate") or 0,
                    "mrp": line.get("mrp") or 0,
                    "total_amount": line.get("total_amount") or 0,
                    "tax_rate": line.get("tax_rate") or 0,
                    "gst_tax": line.get("gst_tax") or "",
                    "igst_tax": line.get("igst_tax") or "",
                    "taxcode": line.get("taxcode") or "",
                },
            )
    found = get_po(conn, po_number, channel)
    if not found:
        raise ValueError("PO not found after update")
    return found


def replace_ship_to_aliases(conn: psycopg.Connection, rows: list[dict[str, str]], channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM b2b.ship_to_aliases WHERE channel = %s", (channel,))
        for row in rows:
            cur.execute(
                "INSERT INTO b2b.ship_to_aliases (channel, address_id, address_entry) VALUES (%s, %s, %s)",
                (channel, row["address_id"], row["address_entry"]),
            )


def get_po(conn: psycopg.Connection, po_number: str, channel: str = CHANNEL_BLINKIT) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM b2b.purchase_orders WHERE channel = %s AND po_number = %s",
            (channel, po_number),
        )
        header = cur.fetchone()
        if not header:
            return None
        cur.execute("SELECT * FROM b2b.po_lines WHERE po_id = %s ORDER BY line_no", (header["id"],))
        lines = list(cur.fetchall())
    return {"header": dict(header), "lines": [dict(r) for r in lines]}


def archive_po_for_amend(
    conn: psycopg.Connection,
    detail: dict[str, Any],
    *,
    email_subject: str = "",
    email_uid: str = "",
) -> str:
    """Snapshot existing PO + lines, clear SAP linkage so the amend re-ingests as a fresh post."""
    header = dict(detail["header"])
    channel = header.get("channel") or CHANNEL_BLINKIT
    po_number = str(header["po_number"])
    po_id = int(header["id"])
    snapshot = {"header": header, "lines": [dict(r) for r in detail.get("lines") or []]}
    record_data = json.loads(json.dumps(snapshot, default=str))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO b2b.po_amend_history (
              channel, po_number, po_id, record_data,
              sap_docentry, sap_docnum, sap_status, sap_document_status, pipeline_status,
              email_subject, email_uid
            ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                channel,
                po_number,
                po_id,
                json.dumps(record_data, default=str),
                header.get("sap_docentry"),
                header.get("sap_docnum"),
                header.get("sap_status") or "",
                header.get("sap_document_status") or "",
                header.get("pipeline_status") or "",
                email_subject or "",
                email_uid or "",
            ),
        )
        cur.execute(
            "SELECT COUNT(*) AS n FROM b2b.po_amend_history WHERE channel = %s AND po_number = %s",
            (channel, po_number),
        )
        amend_count = int(cur.fetchone()["n"])
        amend_status = f"Amended/{amend_count}"
        cur.execute(
            """
            UPDATE b2b.purchase_orders SET
              duplicate_records_status = %s,
              sap_status = '',
              sap_docentry = NULL,
              sap_docnum = NULL,
              sap_document_status = '',
              sap_response = NULL,
              sap_doc_total = NULL,
              sap_doc_date = NULL,
              pipeline_status = %s,
              updated_at = now()
            WHERE id = %s
            """,
            (amend_status, STATUS_PARSED, po_id),
        )
        cur.execute("DELETE FROM b2b.po_lines WHERE po_id = %s", (po_id,))
    return amend_status


def list_po_amend_history(
    conn: psycopg.Connection,
    po_number: str,
    channel: str = CHANNEL_BLINKIT,
    *,
    include_record_data: bool = False,
) -> list[dict[str, Any]]:
    cols = (
        "id, channel, po_number, sap_docentry, sap_docnum, sap_status, sap_document_status, "
        "pipeline_status, email_subject, email_uid, archived_at"
    )
    if include_record_data:
        cols += ", record_data"
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {cols}
            FROM b2b.po_amend_history
            WHERE channel = %s AND po_number = %s
            ORDER BY archived_at DESC
            """,
            (channel, po_number),
        )
        return [dict(r) for r in cur.fetchall()]


def get_po_amend_by_id(conn: psycopg.Connection, amend_id: int, channel: str = CHANNEL_BLINKIT) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM b2b.po_amend_history WHERE id = %s AND channel = %s",
            (amend_id, channel),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def delete_po(conn: psycopg.Connection, po_number: str, channel: str = CHANNEL_BLINKIT) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM b2b.purchase_orders
            WHERE channel = %s AND po_number = %s AND pipeline_status <> 'posted'
            """,
            (channel, po_number),
        )
        return cur.rowcount > 0


def set_po_status(
    conn: psycopg.Connection,
    po_number: str,
    pipeline_status: str,
    *,
    channel: str = CHANNEL_BLINKIT,
    sap_status: str | None = None,
    sap_docentry: int | None = None,
    sap_docnum: int | None = None,
    sap_document_status: str | None = None,
    last_error: str | None = None,
    sap_response: Any = None,
    sap_doc_total: float | None = None,
    sap_doc_date: str | None = None,
) -> None:
    sets = ["pipeline_status = %s", "updated_at = now()"]
    args: list[Any] = [pipeline_status]
    if sap_status is not None:
        sets.append("sap_status = %s")
        args.append(sap_status)
    if sap_docentry is not None:
        sets.append("sap_docentry = %s")
        args.append(sap_docentry)
    if sap_docnum is not None:
        sets.append("sap_docnum = %s")
        args.append(sap_docnum)
    if sap_document_status is not None:
        sets.append("sap_document_status = %s")
        args.append(sap_document_status)
    if last_error is not None:
        sets.append("last_error = %s")
        args.append(last_error)
    if sap_response is not None:
        sets.append("sap_response = %s::jsonb")
        args.append(json.dumps(sap_response, default=str))
    if sap_doc_total is not None:
        sets.append("sap_doc_total = %s")
        args.append(sap_doc_total)
    if sap_doc_date is not None:
        sets.append("sap_doc_date = %s")
        args.append(sap_doc_date or None)
    args.extend([channel, po_number])
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE b2b.purchase_orders SET {', '.join(sets)} WHERE channel = %s AND po_number = %s",
            args,
        )


def add_notification(conn: psycopg.Connection, *, kind: str, title: str, body: str, po_number: str = "", channel: str = CHANNEL_BLINKIT) -> None:
    """One inbox row per PO — replace any prior notification for the same channel + po_number."""
    po = (po_number or "").strip()
    with conn.cursor() as cur:
        if po:
            cur.execute(
                "DELETE FROM b2b.notifications WHERE channel = %s AND po_number = %s",
                (channel, po),
            )
        cur.execute(
            "INSERT INTO b2b.notifications (channel, kind, title, body, po_number) VALUES (%s, %s, %s, %s, %s)",
            (channel, kind, title, body, po),
        )


def _dedupe_notifications(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest notification per PO (rows already sorted newest-first)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in items:
        po = str(row.get("po_number") or "").strip()
        if not po:
            out.append(row)
            continue
        key = (str(row.get("channel") or CHANNEL_BLINKIT), po)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def add_email_log(conn: psycopg.Connection, *, recipients: str, subject: str, po_number: str, sent_ok: bool, error: str = "", channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO b2b.email_log (channel, recipients, subject, po_number, sent_ok, error)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (channel, recipients, subject, po_number or "", sent_ok, error),
        )


def add_audit(conn: psycopg.Connection, *, actor: str, action: str, po_number: str = "", detail: str = "", channel: str = CHANNEL_BLINKIT) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO b2b.audit_log (channel, po_number, actor, action, detail) VALUES (%s, %s, %s, %s, %s)",
            (channel, po_number or "", actor, action, detail),
        )


def list_pos(
    conn: psycopg.Connection,
    *,
    channel: str = CHANNEL_BLINKIT,
    pipeline_status: str | None = None,
    po_number: str | None = None,
    sap_docnum: str | None = None,
    sap_docentry: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    where = ["channel = %s"]
    args: list[Any] = [channel]
    if pipeline_status == STATUS_NEEDS_REVIEW:
        where.append(
            "(pipeline_status = %s OR (coalesce(last_error, '') <> '' AND pipeline_status NOT IN ('posted', 'failed', 'rejected', 'cancelled')))"
        )
        args.append(STATUS_NEEDS_REVIEW)
    elif pipeline_status:
        where.append("pipeline_status = %s")
        args.append(pipeline_status)
    if po_number:
        where.append("po_number ILIKE %s")
        args.append(f"%{po_number}%")
    if sap_docnum:
        where.append("CAST(sap_docnum AS text) ILIKE %s")
        args.append(f"%{sap_docnum.strip()}%")
    if sap_docentry:
        try:
            where.append("sap_docentry = %s")
            args.append(int(sap_docentry))
        except ValueError:
            where.append("1=0")
    if created_from:
        where.append("created_at::date >= %s::date")
        args.append(created_from.strip()[:10])
    if created_to:
        where.append("created_at::date <= %s::date")
        args.append(created_to.strip()[:10])
    sort_cols = {
        "po_number": "po_number",
        "status": "pipeline_status",
        "sap_status": "sap_document_status",
        "sap_docnum": "sap_docnum",
        "sap_docentry": "sap_docentry",
        "amount": "total_amount",
        "items": "total_items",
        "created_at": "created_at",
    }
    order_col = sort_cols.get(str(sort_by or "").strip(), "created_at")
    direction = "ASC" if str(sort_dir or "").lower() == "asc" else "DESC"
    clause = " AND ".join(where)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM b2b.purchase_orders WHERE {clause}", args)
        total = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT * FROM b2b.purchase_orders WHERE {clause} ORDER BY {order_col} {direction} NULLS LAST LIMIT %s OFFSET %s",
            args + [limit, offset],
        )
        items = [dict(r) for r in cur.fetchall()]
    return {"total": total, "items": items, "limit": limit, "offset": offset}


def fetch_stats(conn: psycopg.Connection, channel: str = CHANNEL_BLINKIT) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT CASE
              WHEN pipeline_status = 'failed' THEN 'failed'
              WHEN pipeline_status = 'rejected' THEN 'rejected'
              WHEN pipeline_status = 'cancelled' THEN 'cancelled'
              WHEN pipeline_status = 'posted' AND sap_document_status = 'bost_Close' THEN 'posted_closed'
              WHEN pipeline_status = 'posted' THEN 'posted_open'
              WHEN pipeline_status = 'needs_review'
                OR (coalesce(last_error, '') <> '' AND pipeline_status NOT IN ('posted', 'failed', 'rejected', 'cancelled'))
                THEN 'needs_review'
              WHEN pipeline_status = 'ready' THEN 'pending_approval'
              ELSE pipeline_status
            END AS bucket, COUNT(*)::int
            FROM b2b.purchase_orders
            WHERE channel = %s
            GROUP BY 1
            """,
            (channel,),
        )
        by_status = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM b2b.purchase_orders WHERE channel = %s", (channel,))
        total = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM b2b.notifications WHERE is_read = false AND channel = %s", (channel,))
        unread = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM b2b.sku_map WHERE channel = %s", (channel,))
        sku_count = int(cur.fetchone()[0])
        cur.execute("SELECT COUNT(*) FROM b2b.ship_to_aliases WHERE channel = %s", (channel,))
        ship_count = int(cur.fetchone()[0])
    summary = {
        "pending_approval": by_status.get("pending_approval", 0),
        "needs_review": by_status.get("needs_review", 0),
        "failed": by_status.get("failed", 0),
        "rejected": by_status.get("rejected", 0),
        "posted_open": by_status.get("posted_open", 0),
        "posted_closed": by_status.get("posted_closed", 0),
        "cancelled": by_status.get("cancelled", 0),
    }
    return {
        "total_pos": total,
        "sku_map_count": sku_count,
        "ship_to_count": ship_count,
        "by_pipeline_status": by_status,
        "summary": summary,
        "unread_notifications": unread,
    }


def list_posted_for_sync(conn: psycopg.Connection, *, channel: str = CHANNEL_BLINKIT, limit: int = 200) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT po_number, sap_docentry, sap_docnum, pipeline_status
            FROM b2b.purchase_orders
            WHERE channel = %s
              AND sap_docentry IS NOT NULL
              AND pipeline_status IN ('posted', 'cancelled')
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (channel, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def list_notifications(
    conn: psycopg.Connection,
    *,
    unread_only: bool = False,
    limit: int = 50,
    channel: str = CHANNEL_BLINKIT,
    channels: list[str] | None = None,
) -> list[dict[str, Any]]:
    chans = [str(c).strip().lower() for c in (channels or []) if str(c).strip()]
    if not chans:
        chans = [str(channel or CHANNEL_BLINKIT).strip().lower() or CHANNEL_BLINKIT]
    with conn.cursor(row_factory=dict_row) as cur:
        if unread_only:
            cur.execute(
                """
                SELECT * FROM b2b.notifications
                WHERE channel = ANY(%s) AND is_read = false
                ORDER BY created_at DESC LIMIT %s
                """,
                (chans, limit),
            )
        else:
            cur.execute(
                """
                SELECT * FROM b2b.notifications
                WHERE channel = ANY(%s)
                ORDER BY created_at DESC LIMIT %s
                """,
                (chans, limit),
            )
        return _dedupe_notifications([dict(r) for r in cur.fetchall()])


def mark_notifications_read(
    conn: psycopg.Connection,
    notification_id: int | None = None,
    *,
    po_number: str = "",
    channel: str = CHANNEL_BLINKIT,
) -> None:
    with conn.cursor() as cur:
        if notification_id:
            cur.execute(
                "UPDATE b2b.notifications SET is_read = true WHERE id = %s AND channel = %s",
                (notification_id, channel),
            )
        elif po_number:
            cur.execute(
                "UPDATE b2b.notifications SET is_read = true WHERE po_number = %s AND channel = %s",
                (po_number, channel),
            )
        else:
            cur.execute("UPDATE b2b.notifications SET is_read = true WHERE channel = %s", (channel,))


def mark_notifications_read_for_posted_pos(conn: psycopg.Connection, channel: str = CHANNEL_BLINKIT) -> int:
    """Mark mailbox alerts read when their PO is already posted to SAP."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE b2b.notifications n
            SET is_read = true
            FROM b2b.purchase_orders p
            WHERE n.po_number = p.po_number
              AND n.channel = p.channel
              AND n.channel = %s
              AND n.is_read = false
              AND p.pipeline_status = %s
            """,
            (channel, STATUS_POSTED),
        )
        return cur.rowcount


def delete_notification(conn: psycopg.Connection, notification_id: int, channel: str = CHANNEL_BLINKIT) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM b2b.notifications WHERE id = %s AND channel = %s",
            (notification_id, channel),
        )
        return cur.rowcount > 0


def get_user_by_email(conn: psycopg.Connection, email: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT u.email, u.full_name, u.role, u.sap_salesperson_code, u.is_active,
                   COALESCE(u.channels, ARRAY['blinkit']::text[]) AS channels,
                   COALESCE(u.perm_orders, '') AS perm_orders,
                   COALESCE(u.perm_sku_map, '') AS perm_sku_map,
                   COALESCE(u.perm_ship_to, '') AS perm_ship_to,
                   COALESCE(u.perm_delivery, '') AS perm_delivery,
                   r.can_approve, r.can_post_sap, r.can_manage_users
            FROM b2b.users u
            JOIN b2b.roles r ON r.role = u.role
            WHERE lower(u.email) = lower(%s)
            """,
            (email.strip(),),
        )
        row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    data["channels"] = _normalize_channels(data.get("channels"), data.get("role") or "")
    return permissions.attach_permissions(data)


def list_users(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT u.email, u.full_name, u.role, u.sap_salesperson_code, u.is_active,
                   COALESCE(u.channels, ARRAY['blinkit']::text[]) AS channels,
                   COALESCE(u.perm_orders, '') AS perm_orders,
                   COALESCE(u.perm_sku_map, '') AS perm_sku_map,
                   COALESCE(u.perm_ship_to, '') AS perm_ship_to,
                   COALESCE(u.perm_delivery, '') AS perm_delivery,
                   r.can_approve, r.can_post_sap, r.can_manage_users
            FROM b2b.users u
            JOIN b2b.roles r ON r.role = u.role
            ORDER BY u.email
            """
        )
        items = []
        for r in cur.fetchall():
            data = dict(r)
            data["channels"] = list(data.get("channels") or [CHANNEL_BLINKIT])
            items.append(permissions.attach_permissions(data))
        return items


def upsert_user(
    conn: psycopg.Connection,
    *,
    email: str,
    full_name: str,
    role: str,
    sap_salesperson_code: str,
    is_active: bool = True,
    channels: list[str] | None = None,
    perm_orders: str = "",
    perm_sku_map: str = "",
    perm_ship_to: str = "",
    perm_delivery: str = "",
) -> None:
    assigned = _normalize_channels(channels, role)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO b2b.users (email, full_name, role, sap_salesperson_code, is_active, channels,
              perm_orders, perm_sku_map, perm_ship_to, perm_delivery, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (email) DO UPDATE SET
              full_name = EXCLUDED.full_name,
              role = EXCLUDED.role,
              sap_salesperson_code = EXCLUDED.sap_salesperson_code,
              is_active = EXCLUDED.is_active,
              channels = EXCLUDED.channels,
              perm_orders = EXCLUDED.perm_orders,
              perm_sku_map = EXCLUDED.perm_sku_map,
              perm_ship_to = EXCLUDED.perm_ship_to,
              perm_delivery = EXCLUDED.perm_delivery,
              updated_at = now()
            """,
            (
                email.strip().lower(),
                full_name.strip(),
                role,
                sap_salesperson_code.strip(),
                is_active,
                assigned,
                permissions.normalize_level(perm_orders),
                permissions.normalize_level(perm_sku_map),
                permissions.normalize_level(perm_ship_to),
                permissions.normalize_level(perm_delivery),
            ),
        )


def set_user_active(conn: psycopg.Connection, email: str, is_active: bool) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE b2b.users SET is_active = %s, updated_at = now() WHERE lower(email) = lower(%s)",
            (is_active, email.strip()),
        )
        return cur.rowcount > 0


def list_audit(conn: psycopg.Connection, *, limit: int = 150, channel: str | None = None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        if channel:
            cur.execute(
                """
                SELECT actor, action, po_number, detail, channel, created_at
                FROM b2b.audit_log
                WHERE channel = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (channel, limit),
            )
        else:
            cur.execute(
                """
                SELECT actor, action, po_number, detail, channel, created_at
                FROM b2b.audit_log
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
        return [dict(r) for r in cur.fetchall()]


def list_po_audit(
    conn: psycopg.Connection,
    po_number: str,
    *,
    channel: str = CHANNEL_BLINKIT,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT actor, action, po_number, detail, channel, created_at
            FROM b2b.audit_log
            WHERE channel = %s AND po_number = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (channel, po_number, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def delete_user(conn: psycopg.Connection, email: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM b2b.users WHERE lower(email) = lower(%s)", (email.strip(),))
        return cur.rowcount > 0

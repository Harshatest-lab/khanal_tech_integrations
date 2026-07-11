#!/usr/bin/env python3
"""Preview SAP Invoice payload for a Walmart PO (local only, no post)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
_REPO_SHARED = Path(__file__).resolve().parent.parent.parent.parent / "shared"
sys.path.insert(0, str(_SRC))
if _REPO_SHARED.is_dir():
    sys.path.insert(0, str(_REPO_SHARED))

import psycopg

from walmart.credentials import DATABASE_URL
from walmart.sap_invoice_payload import build_sap_invoice_payload


def _load_order(conn: psycopg.Connection, purchase_order_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT raw_payload
            FROM walmart_order_headers
            WHERE purchase_order_id = %s
            """,
            (purchase_order_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"PO not found: {purchase_order_id}")

    payload_json = row[0]
    if isinstance(payload_json, str):
        data = json.loads(payload_json)
    else:
        data = payload_json
    order = data.get("order") or data
    if not isinstance(order, dict):
        raise SystemExit(f"Invalid order JSON for PO {purchase_order_id}")
    return order


def main() -> int:
    parser = argparse.ArgumentParser(description="Build SAP Invoice payload from stored Walmart order JSON.")
    parser.add_argument("purchase_order_id", help="Walmart purchase order id")
    parser.add_argument(
        "--skip-batches",
        action="store_true",
        help="Skip HANA batch lookup and use placeholder batch numbers",
    )
    args = parser.parse_args()

    if not DATABASE_URL:
        print("Error: DATABASE_URL not set", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(DATABASE_URL) as conn:
            order = _load_order(conn, args.purchase_order_id)
            require_batches = not args.skip_batches
            try:
                payload = build_sap_invoice_payload(
                    conn, order, require_batches=require_batches
                )
            except ValueError as exc:
                msg = str(exc)
                if require_batches and "Insufficient batch stock" in msg:
                    print(f"Insufficient batch stock: {msg}", file=sys.stderr)
                    print(
                        "Preview will continue with placeholder batch "
                        "(PREVIEW_NO_BATCH_STOCK). Add stock in SAP warehouse WM-FBA "
                        "before real SAP posting.",
                        file=sys.stderr,
                    )
                    print(file=sys.stderr)
                    payload = build_sap_invoice_payload(
                        conn, order, require_batches=False
                    )
                else:
                    print(f"Error: {msg}", file=sys.stderr)
                    return 1
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.skip_batches:
        print(
            "Note: --skip-batches used; BatchNumbers may show PREVIEW_NO_BATCH_STOCK.",
            file=sys.stderr,
        )

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

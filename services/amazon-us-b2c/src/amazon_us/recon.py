"""Reconciliation: daily pipeline invariants + monthly MTR recon.

Daily invariants ("nothing silently missing"):
  - every Shipped staged order with a ship date [cutoff .. today-2] must
    be posted, failed (with a reason), or explainably waiting
  - no order stuck in 'posting' (crashed worker)
  - failed orders that exhausted retries
  - CN failures, and no CN claim stuck in 'posting' (crashed worker)
  - fetch watermark freshness (a silent-dead fetch is an outage)
Breaches alert via SES; counts land in amazon_us_sync_log.

Monthly MTR recon (the Finance-facing gate): parse the Seller Central
"Custom (Unified) Transaction" CSV, aggregate buyer-facing GROSS amounts
per order, pull the SAP side per order from HANA (pipeline docs only —
NumAtCard matching the Amazon order-id shape, CANCELED='N'), compare to
the cent, categorize every difference. Result CSV goes to S3; summary
goes out via SES.

Matching rules baked in from the June 2026 close (99.55% matched):
  - SAP CN NumAtCard is "{order_id}-{timestamp}" — strip the suffix
    before joining, or every CN looks like an orphan.
  - Compare buyer-facing gross columns (product sales + tax + shipping
    + gift wrap), never MTR's fee-netted "total".
  - CNs aggregate PER ORDER (Finances PostedDate never equals the MTR
    refund timestamp, so event-level joins can't work).
  - CN SAP pull window extends past the period (+45d): CN DocDates lag
    the MTR month.
  - "Refund in MTR but sale missing in SAP" for a pre-cutoff order is
    EXPECTED (Finance booked pre-cutoff revenue manually).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from amazon_us import constants, store
from amazon_us.alerts import send_alert

logger = logging.getLogger(__name__)

_ORDER_ID_RE = re.compile(r"^\d{3}-\d{7}-\d{7}$")
_ORDER_ID_PREFIX_RE = re.compile(r"^(\d{3}-\d{7}-\d{7})(?:-.+)?$")
_AMOUNT_TOLERANCE = 0.02


# ── Daily invariants ─────────────────────────────────────────────────────

def run_invariants(conn) -> dict:
    run_id = store.start_sync_log(conn, "recon")
    try:
        horizon = (date.today() - timedelta(days=2)).isoformat()
        checks: dict[str, Any] = {}

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM amazon_us_orders
                WHERE order_status = 'Shipped'
                  AND actual_ship_date BETWEEN %s::date AND %s::date
                  AND stage NOT IN ('posted', 'failed')
                """,
                (constants.POSTING_CUTOFF_DATE, horizon),
            )
            checks["shipped_not_posted"] = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM amazon_us_orders "
                "WHERE stage = 'posting' AND updated_at < now() - interval '30 minutes'"
            )
            checks["stuck_posting"] = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM amazon_us_orders WHERE stage = 'failed' AND attempt_count >= 3"
            )
            checks["failed_exhausted"] = cur.fetchone()[0]

            cur.execute("SELECT count(*) FROM amazon_us_cn_log WHERE status = 'failed'")
            checks["cn_failed"] = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM amazon_us_cn_log "
                "WHERE status = 'posting' AND updated_at < now() - interval '30 minutes'"
            )
            checks["cn_stuck_posting"] = cur.fetchone()[0]

            cur.execute(
                "SELECT count(*) FROM amazon_us_orders "
                "WHERE stage = 'fetched' AND last_error LIKE 'Unmapped SKUs%'"
            )
            checks["unmapped_sku_orders"] = cur.fetchone()[0]

        watermark = store.get_watermark(conn, "orders_last_updated_after")
        watermark_age_h: float | None = None
        if watermark:
            wm = datetime.fromisoformat(watermark.replace(" ", "T"))
            if wm.tzinfo is None:
                wm = wm.replace(tzinfo=timezone.utc)
            watermark_age_h = round(
                (datetime.now(timezone.utc) - wm).total_seconds() / 3600, 1
            )
        checks["fetch_watermark_age_hours"] = watermark_age_h

        breaches = []
        if checks["stuck_posting"]:
            breaches.append(f"{checks['stuck_posting']} orders stuck in 'posting' >30min")
        if checks["failed_exhausted"]:
            breaches.append(f"{checks['failed_exhausted']} orders failed with retries exhausted")
        if checks["cn_failed"]:
            breaches.append(f"{checks['cn_failed']} credit-note events in failed state")
        if checks["cn_stuck_posting"]:
            breaches.append(f"{checks['cn_stuck_posting']} credit-note events stuck in 'posting' >30min")
        if watermark_age_h is None or watermark_age_h > 6:
            breaches.append(f"order-fetch watermark stale ({watermark_age_h}h) — fetch may be dead")
        if checks["shipped_not_posted"] > 50:
            breaches.append(
                f"{checks['shipped_not_posted']} shipped orders older than 2 days not posted/failed"
            )

        checks["breaches"] = breaches
        if breaches:
            send_alert(
                "pipeline invariant breach",
                "Daily invariant check found:\n- " + "\n- ".join(breaches) + f"\n\nFull counts: {checks}",
            )
        store.finish_sync_log(conn, run_id, "success", checks)
        return {"status": "done", **checks}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise


# ── MTR parsing (pure) ───────────────────────────────────────────────────

def parse_mtr(content: str) -> dict[str, dict]:
    """Aggregate the Custom Unified Transaction CSV per order:
    {order_id: {'sales': x, 'refunds': y}} using buyer-facing gross
    columns. Header row is located by scanning for '"date/time"' (files
    ship with preamble lines above it)."""
    lines = content.splitlines(keepends=True)
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith('"date/time"') or line.strip().startswith("date/time"):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError('Could not find the MTR column header row (expected "date/time")')

    reader = csv.DictReader(io.StringIO("".join(lines[header_idx:])))
    orders: dict[str, dict] = {}

    def _f(row: dict, key: str) -> float:
        try:
            return float(str(row.get(key) or "0").replace(",", ""))
        except ValueError:
            return 0.0

    for row in reader:
        row_type = (row.get("type") or "").strip()
        order_id = (row.get("order id") or "").strip()
        if not order_id or row_type not in ("Order", "Refund"):
            continue
        gross = (
            _f(row, "product sales")
            + _f(row, "product sales tax")
            + _f(row, "shipping credits")
            + _f(row, "shipping credits tax")
            + _f(row, "gift wrap credits")
            + _f(row, "giftwrap credits tax")
        )
        bucket = orders.setdefault(order_id, {"sales": 0.0, "refunds": 0.0})
        if row_type == "Order":
            bucket["sales"] = round(bucket["sales"] + gross, 2)
        else:
            bucket["refunds"] = round(bucket["refunds"] + abs(gross), 2)
    return orders


# ── SAP side (HANA) ──────────────────────────────────────────────────────

def fetch_sap_side(period_from: str, period_to: str) -> dict[str, dict]:
    """{order_id: {'sales': DocTotal-sum, 'refunds': CN-total-sum}} for
    pipeline documents. CN window extends +45 days past the period."""
    from khanal_shared.hana.pool import hana_connection

    cn_to = (datetime.strptime(period_to, "%Y-%m-%d") + timedelta(days=45)).strftime("%Y-%m-%d")
    result: dict[str, dict] = {}

    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT "NumAtCard", SUM("DocTotal")
            FROM "OINV"
            WHERE "CANCELED" = 'N' AND "DocDate" BETWEEN ? AND ?
              AND "NumAtCard" LIKE '___-_______-_______'
            GROUP BY "NumAtCard"
            ''',
            [period_from, period_to],
        )
        for num_at_card, total in cur.fetchall() or []:
            oid = str(num_at_card).strip()
            if _ORDER_ID_RE.match(oid):
                result.setdefault(oid, {"sales": 0.0, "refunds": 0.0})["sales"] = float(total or 0)

        cur.execute(
            '''
            SELECT "NumAtCard", SUM("DocTotal")
            FROM "ORIN"
            WHERE "CANCELED" = 'N' AND "DocDate" BETWEEN ? AND ?
              AND "NumAtCard" LIKE '___-_______-_______%'
            GROUP BY "NumAtCard"
            ''',
            [period_from, cn_to],
        )
        for num_at_card, total in cur.fetchall() or []:
            raw = str(num_at_card).strip()
            prefix_match = _ORDER_ID_PREFIX_RE.match(raw)
            oid = prefix_match.group(1) if prefix_match else raw
            if _ORDER_ID_RE.match(oid):
                bucket = result.setdefault(oid, {"sales": 0.0, "refunds": 0.0})
                bucket["refunds"] = round(bucket["refunds"] + float(total or 0), 2)
        cur.close()
    return result


# ── Finance's own Revenue Recognition query (SAP HANA, unmodified) ──────
#
# Pasted verbatim from Finance's SAP HANA report (2026-08-12) with the
# `[%0]`/`[%1]` Crystal-style date parameters swapped for `?` placeholders
# and one addition: `AND A."CardCode" = ?` in both branches, so this can be
# scoped to Amazon US B2C (CardCode C00001) instead of the whole company.
# Deliberately NOT reimplemented in Python — Taxable Value / Discount /
# Freight / COGS / Gross Margin / channel (GroupCode) are Finance's own
# formulas; keeping the SQL untouched means this can never quietly drift
# from what Finance's pivot actually computes. DocDate (= ship date) is
# already the window field on both sides, matching amazon_us's own
# actual_ship_date-based posting rule (see [[fetch_sap_side]] above).
FINANCE_REVENUE_QUERY = '''
SELECT
    'KF Inc. US' as "Company",
    'No' as "RR-KFPL-India",
    'Yes' as "RR-KFPL-USA",
    CASE WHEN X."CardCode" = 'C00009' THEN 'No' else 'Yes' end as "RR-KFPL-CFS",
    CASE WHEN X."CardCode" = 'C00009' THEN 'No' else 'Yes' end as "Revenue Recognition",
    TO_VARCHAR(X."DocDate",'DD-Mon-YYYY') as "Document Date",
    TO_VARCHAR(X."DocDate", 'Mon-YY') AS "Month",
    CASE WHEN X."ObjType" = '13' then 'SALES INVOICE'
         WHEN X."ObjType" = '14' THEN 'CREDIT MEMO'
         ELSE 'Others' end as "Document",
    X."NumAtCard" as "BP Reference No.",
    X."DocNum" as "Document Number",
    X."SeriesName" as "Series",
    X."KFIN DocNum" as "Document No",
    X."FormatCode" as "G/L Code",
    X."AcctName" as "G/L Name",
    X."CardCode",
    X."CardName",
    X."CardFName" as "Common Name",
    case when x."GroupCode" = '100' then 'E-com'
         WHEN X."GroupCode" = '102' THEN 'Export'
         WHEN X."GroupCode" = '103' THEN 'Offline Dogsee'
         WHEN X."GroupCode" = '104' THEN 'Other Customer'
         else 'Others' end as "Channel",
    X."State",
    X."Currency",
    X."DocRate",
    X."Dscription",
    X."Actual ItemName",
    X."Actual ItemCode",
    X."Product Category",
    X."Brand Category",
    X."Brand Name",
    CASE WHEN X."Country" = 'USA' then 'Domestic' else 'International' end as "Geography",
    X."Country",
    X."U_UoM_MIS" as "UoM",
    X."Quantity",
    X."PriceBefDi" as "Unit Price",
    (X."Quantity" * X."PriceBefDi") as "Taxable Value",
    (IFNULL(X."PriceBefDi",0) * IFNULL(X."Quantity",0) * IFNULL(X."DiscPrcnt",0)) / 100 AS "Discount Value",
    CASE
        WHEN X."ObjType" = '13' THEN
            IFNULL(X."Freight",0) + IFNULL(X."Taxable Value",0) - (IFNULL(X."DiscPrcnt",0)) / 100
        WHEN X."ObjType" = '14' THEN
            IFNULL(X."Freight",0) + IFNULL(X."Taxable Value",0) + (IFNULL(X."DiscPrcnt",0)) / 100
        ELSE 0
    END AS "Net Taxable Value",
    IFNULL(X."Freight",0) as "Freight",
    X."COGS per unit",
    X."COGS",
    X."Gross Margin",
    TO_DECIMAL(X."Gross Margin" / NULLIF(X."Taxable Value", 0), 10, 4) * 100 || '%' AS "Gross Margin %",
    CASE
        WHEN X."WhsName" LIKE 'ShipCube%' THEN 'ShipCube'
        WHEN X."WhsName" LIKE 'Wow%' THEN 'WowPets'
        ELSE X."WhsName"
    END AS "Location",
    X."Comments"
FROM
(
    ----------------------------------------------------------------------
    -- AR INVOICE
    ----------------------------------------------------------------------
    SELECT
        A."Comments", W."WhsName", B."LineNum",
        'AR Invoice' AS "DocTypeName",
        A."DocEntry", A."DocType" AS "Item/Service", A."DocDate", A."ObjType", A."DocNum", A."DocCur",
        I."U_BC" as "Brand Category", I."U_BN" as "Brand Name", I."U_PSC" as "Product Category",
        I."U_UoM_MIS", A."DocRate", BP."GroupCode", BP."Currency", SE."SeriesName", BP."CardFName", A."DocStatus",
        CASE WHEN IFNULL(SE."SeriesName",'') = '' THEN TO_VARCHAR(A."DocNum")
             ELSE SE."SeriesName" || '/' || A."DocNum" END AS "KFIN DocNum",
        B."AcctCode" AS "G/L Code", AB."FormatCode", AB."AcctName",
        A."CardCode", A."CardName",
        S."Name" AS "State", CO."Name" as "Country",
        A."NumAtCard", B."ItemCode", B."Dscription",
        B."StockPrice" AS "COGS per unit", B."DiscPrcnt",
        CASE WHEN A."DocType" = 'I' THEN (B."StockPrice" * B."Quantity") ELSE 0 END AS "COGS",
        (
            SELECT T."TotalFreight" / NULLIF(T."TotalLines",0)
            FROM (
                SELECT COUNT(*) AS "TotalLines", MAX(I3."LineTotal") AS "TotalFreight"
                FROM INV1 I1
                LEFT JOIN INV3 I3 ON I1."DocEntry" = I3."DocEntry" AND I3."ExpnsCode" = 1
                WHERE I1."DocEntry" = B."DocEntry"
            ) T
        ) AS "Freight",
        (
            CASE WHEN A."DocType" = 'I' THEN (B."Quantity" * B."Price") ELSE B."Price" END
            - CASE WHEN A."DocType" = 'I' THEN (B."StockPrice" * B."Quantity") ELSE 0 END
        ) AS "Gross Margin",
        CASE WHEN IFNULL(B."U_ActualItem", '') = '' THEN B."ItemCode" ELSE B."U_ActualItem" END AS "Actual ItemCode",
        CASE WHEN IFNULL(C."ItemName", '') = '' THEN B."Dscription" ELSE C."ItemName" END AS "Actual ItemName",
        B."Quantity", B."PriceBefDi",
        CASE WHEN A."DocType" = 'I' THEN (B."Quantity" * B."Price") ELSE B."Price" END AS "Taxable Value"
    FROM OINV A
    LEFT JOIN NNM1 SE ON SE."Series" = A."Series"
    INNER JOIN INV1 B ON A."DocEntry" = B."DocEntry"
    INNER JOIN OITM I ON B."ItemCode" = I."ItemCode"
    INNER JOIN OWHS W ON B."WhsCode" = W."WhsCode"
    LEFT JOIN OCRD BP ON A."CardCode" = BP."CardCode"
    LEFT JOIN CRD1 AD ON BP."CardCode" = AD."CardCode" AND AD."Address" = A."ShipToCode" AND AD."AdresType" = 'S'
    LEFT JOIN OCST S ON S."Code" = AD."State" AND S."Country" = AD."Country"
    LEFT JOIN OCRY CO ON CO."Code" = AD."Country"
    INNER JOIN OACT AB ON B."AcctCode" = AB."AcctCode"
    LEFT JOIN OITM C ON C."ItemCode" = B."U_ActualItem"
    WHERE A."CANCELED" = 'N' AND A."DocDate" BETWEEN ? AND ? AND A."CardCode" = ?

    UNION ALL

    ----------------------------------------------------------------------
    -- AR CREDIT MEMO
    ----------------------------------------------------------------------
    SELECT
        A."Comments", W."WhsName", B."LineNum",
        'AR Credit Memo' AS "DocTypeName",
        A."DocEntry", A."DocType" AS "Item/Service", A."DocDate", A."ObjType", A."DocNum", A."DocCur",
        I."U_BC" as "Brand Category", I."U_BN" as "Brand Name", I."U_PSC" as "Product Category",
        I."U_UoM_MIS", A."DocRate", BP."GroupCode", BP."Currency", SE."SeriesName", BP."CardFName", A."DocStatus",
        CASE WHEN IFNULL(SE."SeriesName",'') = '' THEN TO_VARCHAR(A."DocNum")
             ELSE SE."SeriesName" || '/' || A."DocNum" END AS "KFIN DocNum",
        B."AcctCode" AS "G/L Code", AB."FormatCode", AB."AcctName",
        A."CardCode", A."CardName",
        S."Name" AS "State", CO."Name" as "Country",
        A."NumAtCard", B."ItemCode", B."Dscription",
        B."StockPrice" AS "COGS per unit", B."DiscPrcnt",
        CASE WHEN A."DocType" = 'I' THEN -(B."StockPrice" * B."Quantity") ELSE 0 END AS "COGS",
        (
            SELECT -(T."TotalFreight" / NULLIF(T."TotalLines",0))
            FROM (
                SELECT COUNT(*) AS "TotalLines", MAX(R3."LineTotal") AS "TotalFreight"
                FROM RIN1 R1
                LEFT JOIN RIN3 R3 ON R1."DocEntry" = R3."DocEntry" AND R3."ExpnsCode" = 1
                WHERE R1."DocEntry" = B."DocEntry"
            ) T
        ) AS "Freight",
        (
            (CASE WHEN A."DocType" = 'I' THEN -(B."Quantity" * B."Price") ELSE -(B."Price") END)
            - CASE WHEN A."DocType" = 'I' THEN -(B."StockPrice" * B."Quantity") ELSE 0 END
        ) AS "Gross Margin",
        CASE WHEN IFNULL(B."U_ActualItem", '') = '' THEN B."ItemCode" ELSE B."U_ActualItem" END AS "Actual ItemCode",
        CASE WHEN IFNULL(C."ItemName", '') = '' THEN B."Dscription" ELSE C."ItemName" END AS "Actual ItemName",
        -(B."Quantity") AS "Quantity", B."PriceBefDi",
        CASE WHEN A."DocType" = 'I' THEN -(B."Quantity" * B."Price") ELSE -(B."Price") END AS "Taxable Value"
    FROM ORIN A
    LEFT JOIN NNM1 SE ON SE."Series" = A."Series"
    INNER JOIN RIN1 B ON A."DocEntry" = B."DocEntry"
    INNER JOIN OITM I ON B."ItemCode" = I."ItemCode"
    INNER JOIN OWHS W ON W."WhsCode" = B."WhsCode"
    LEFT JOIN OCRD BP ON A."CardCode" = BP."CardCode"
    LEFT JOIN CRD1 AD ON BP."CardCode" = AD."CardCode" AND AD."Address" = A."ShipToCode" AND AD."AdresType" = 'S'
    LEFT JOIN OCST S ON S."Code" = AD."State" AND S."Country" = AD."Country"
    LEFT JOIN OCRY CO ON CO."Code" = AD."Country"
    INNER JOIN OACT AB ON B."AcctCode" = AB."AcctCode"
    LEFT JOIN OITM C ON C."ItemCode" = B."U_ActualItem"
    WHERE A."CANCELED" = 'N' AND A."DocDate" BETWEEN ? AND ? AND A."CardCode" = ?
) X
GROUP BY
    X."LineNum", X."DocEntry", X."Item/Service", X."DocDate", X."NumAtCard", X."DocNum", X."KFIN DocNum",
    X."DocTypeName", X."FormatCode", X."AcctName", X."CardCode", X."CardName", X."State", X."Country",
    X."Dscription", X."Actual ItemCode", X."Actual ItemName", X."Quantity", X."PriceBefDi", x."DiscPrcnt",
    X."Freight", X."Taxable Value", X."COGS per unit", X."COGS", X."Gross Margin", x."ObjType", x."DocStatus",
    X."CardFName", X."GroupCode", X."Currency", X."DocRate", X."SeriesName", X."Brand Category", X."Brand Name",
    X."Product Category", X."U_UoM_MIS", X."WhsName", X."Comments"
'''


def fetch_finance_revenue_rows(period_from: str, period_to: str, card_code: str = constants.SAP_CARD_CODE) -> list[dict]:
    """Runs Finance's own Revenue Recognition SQL (see FINANCE_REVENUE_QUERY
    above) filtered to `card_code`, returning one dict per row with Finance's
    exact column names (including "Net Taxable Value", "Freight", "COGS",
    "Gross Margin", "Channel") — not our own recomputation of any of it."""
    from khanal_shared.hana.pool import hana_connection

    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(FINANCE_REVENUE_QUERY, [period_from, period_to, card_code, period_from, period_to, card_code])
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
        cur.close()
    return rows


# ── Comparison (pure) ────────────────────────────────────────────────────

def compare(mtr: dict[str, dict], sap: dict[str, dict], cutoff: str, order_dates: dict[str, str]) -> list[dict]:
    """One row per order id seen on either side, categorized."""
    rows = []
    for oid in sorted(set(mtr) | set(sap)):
        m = mtr.get(oid, {"sales": 0.0, "refunds": 0.0})
        s = sap.get(oid, {"sales": 0.0, "refunds": 0.0})
        sales_diff = round(m["sales"] - s["sales"], 2)
        refund_diff = round(m["refunds"] - s["refunds"], 2)

        if abs(sales_diff) <= _AMOUNT_TOLERANCE and abs(refund_diff) <= _AMOUNT_TOLERANCE:
            category = "matched"
        elif oid not in sap and (order_dates.get(oid) or "9999") < cutoff:
            category = "pre_cutoff_excluded"  # Finance booked manually; expected
        elif oid not in sap:
            category = "missing_in_sap"
        elif oid not in mtr:
            category = "sap_not_in_mtr_timing"  # lands in an adjacent MTR month
        elif m["sales"] == 0 and s["sales"] > 0 and abs(refund_diff) <= _AMOUNT_TOLERANCE:
            category = "sap_not_in_mtr_timing"
        elif abs(sales_diff) <= _AMOUNT_TOLERANCE and refund_diff > 0:
            category = "refund_not_yet_posted"  # Finances PostedDate lag
        elif abs(sales_diff) <= _AMOUNT_TOLERANCE and refund_diff < 0:
            category = "refunded_after_mtr_extract"
        else:
            category = "amount_mismatch"

        rows.append(
            {
                "order_id": oid,
                "mtr_sales": m["sales"],
                "sap_sales": s["sales"],
                "sales_diff": sales_diff,
                "mtr_refunds": m["refunds"],
                "sap_refunds": s["refunds"],
                "refund_diff": refund_diff,
                "category": category,
            }
        )
    return rows


def run_mtr_recon(conn, s3_bucket: str, s3_key: str, period_from: str, period_to: str) -> dict:
    """Parse an MTR CSV from S3, compare against SAP via HANA, write the
    detail CSV back to S3, email the summary."""
    import boto3

    run_id = store.start_sync_log(conn, "mtr_recon", period_from, period_to)
    try:
        s3 = boto3.client("s3")
        content = s3.get_object(Bucket=s3_bucket, Key=s3_key)["Body"].read().decode("utf-8-sig")

        mtr = parse_mtr(content)
        sap = fetch_sap_side(period_from, period_to)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT amazon_order_id, COALESCE(actual_ship_date::text, purchase_date::text) "
                "FROM amazon_us_orders"
            )
            order_dates = {oid: (d or "")[:10] for oid, d in cur.fetchall()}

        rows = compare(mtr, sap, constants.POSTING_CUTOFF_DATE, order_dates)

        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()) if rows else ["order_id"])
        writer.writeheader()
        writer.writerows(rows)
        result_key = f"recon/amazon-us-mtr-recon_{period_from}_{period_to}.csv"
        s3.put_object(Bucket=s3_bucket, Key=result_key, Body=out.getvalue().encode("utf-8"))

        summary: dict[str, int] = {}
        for row in rows:
            summary[row["category"]] = summary.get(row["category"], 0) + 1
        total = len(rows)
        matched = summary.get("matched", 0)
        pct = round(100 * matched / total, 2) if total else 0.0

        counts = {"total_orders": total, "matched": matched, "matched_pct": pct,
                  "result_key": result_key, **summary}
        send_alert(
            f"MTR recon {period_from}..{period_to}: {matched}/{total} matched ({pct}%)",
            f"Category breakdown: {summary}\nDetail: s3://{s3_bucket}/{result_key}\n\n"
            "Expected categories (no action): sap_not_in_mtr_timing, "
            "refunded_after_mtr_extract, pre_cutoff_excluded.\n"
            "Action needed: missing_in_sap, amount_mismatch, refund_not_yet_posted "
            "(re-run CN posting with a wider window first).",
        )
        store.finish_sync_log(conn, run_id, "success", counts)
        return {"status": "done", **counts}
    except Exception as e:
        store.finish_sync_log(conn, run_id, "error", error=str(e))
        raise

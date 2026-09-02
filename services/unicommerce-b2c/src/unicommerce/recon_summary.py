"""On-demand, channel-level Unicommerce-vs-SAP reconciliation summary.

Ported from a local Desktop script (run_unicommerce_recon.py) that
required manual DATABASE_URL/HANA env vars and Excel/xlwings automation
to produce a monthly .xlsm reconciliation file -- validated against real
August 2026 data before this port, then moved here so the same numbers
are available on demand from the dashboard, no manual steps.

Distinct from recon.py: that module verifies "did SAP accept what we
already submitted" per order, against SAP Service Layer, and writes
diff columns back onto unicommerce_orders. This module independently
recomputes the correct taxable amounts from raw UC data and compares
against Finance's own Revenue Recognition HANA query (the same one
amazon_us/recon.py already runs from a deployed Lambda via
export_finance_revenue_report -- proven safe to run from here, not just
locally) -- a different, complementary check: "does what we posted
match what Finance's own report shows," aggregated by channel instead
of per-order.

Three corrections found and validated live against August 2026 data,
all necessary for the numbers to actually match (see each site for the
concrete evidence):
  1. Shipping charge is tax-inclusive on our side but SAP/Finance record
     its taxable (pre-tax) value -- strip shipping's own tax before
     comparing, same formula sap_payload.py's compute_tax_from_line uses
     when actually posting the invoice.
  2. Shipping's own declared tax% can be an explicit 0 (not missing) on
     some channels (e.g. AMAZON_IN_API) -- falls back to the item's own
     tax rate, mirroring sap_payload._resolve_shipping_tax_percent.
  3. Finance's own query represents a fully-discounted BOGO/free line's
     Net Taxable Value as a fixed -1 instead of 0 -- corrected here only
     in our own reconciliation math, not in Finance's query.
  4. Credit memos are recorded as negative revenue by Finance's own
     convention while our own computed totals are always a positive
     magnitude -- compare against the magnitude, not the raw signed
     value.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

_AMOUNT_TOLERANCE = 0.02


# ── CardCode scope (dynamic, from live settings) ───────────────────────────

def _card_codes() -> list[str]:
    from unicommerce.settings import get_settings

    settings = get_settings()
    codes = sorted({str(v).strip() for v in settings.channel_customer_map.values() if str(v).strip()})
    if not codes:
        raise RuntimeError(
            "channel_customer_map in /unicommerce/settings is empty -- "
            "no SAP CardCodes to scope the Finance query to"
        )
    return codes


# ── Finance's own Revenue Recognition query (SAP HANA) ─────────────────────
# Copied verbatim from amazon_us/recon.py's FINANCE_REVENUE_QUERY, same two
# deliberate changes as the local script this was ported from: CardCode = ?
# -> CardCode IN (...) in both branches, and X."DocEntry" added to the
# outer SELECT (harmless -- already a GROUP BY key).

_FINANCE_REVENUE_QUERY_TMPL = '''
SELECT
    'KF Inc. US' as "Company",
    'No' as "RR-KFPL-India",
    'Yes' as "RR-KFPL-USA",
    CASE WHEN X."CardCode" = 'C00009' THEN 'No' else 'Yes' end as "RR-KFPL-CFS",
    CASE WHEN X."CardCode" = 'C00009' THEN 'No' else 'Yes' end as "Revenue Recognition",
    X."DocEntry" as "Doc Entry",
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
    WHERE A."CANCELED" = 'N' AND A."DocDate" BETWEEN ? AND ? AND A."CardCode" IN ({card_placeholders})

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
    WHERE A."CANCELED" = 'N' AND A."DocDate" BETWEEN ? AND ? AND A."CardCode" IN ({card_placeholders})
) X
GROUP BY
    X."LineNum", X."DocEntry", X."Item/Service", X."DocDate", X."NumAtCard", X."DocNum", X."KFIN DocNum",
    X."DocTypeName", X."FormatCode", X."AcctName", X."CardCode", X."CardName", X."State", X."Country",
    X."Dscription", X."Actual ItemCode", X."Actual ItemName", X."Quantity", X."PriceBefDi", x."DiscPrcnt",
    X."Freight", X."Taxable Value", X."COGS per unit", X."COGS", X."Gross Margin", x."ObjType", x."DocStatus",
    X."CardFName", X."GroupCode", X."Currency", X."DocRate", X."SeriesName", X."Brand Category", X."Brand Name",
    X."Product Category", X."U_UoM_MIS", X."WhsName", X."Comments"
'''


def _fetch_finance_revenue_rows(period_from: str, period_to: str, card_codes: list[str]) -> list[dict]:
    from khanal_shared.hana.pool import hana_connection

    placeholders = ",".join("?" for _ in card_codes)
    sql = _FINANCE_REVENUE_QUERY_TMPL.format(card_placeholders=placeholders)
    params = [period_from, period_to, *card_codes, period_from, period_to, *card_codes]

    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall() or []]
        cur.close()
    return rows


def _sap_totals_by_doc_entry(finance_rows: list[dict]) -> dict[int, dict]:
    """{doc_entry: {'document': 'SALES INVOICE'|'CREDIT MEMO', 'ntv_sum': float}}."""
    out: dict[int, dict] = {}
    for r in finance_rows:
        de = r.get("Doc Entry")
        if de is None:
            continue
        de = int(de)
        ntv = float(r.get("Net Taxable Value") or 0)
        # Fully-discounted BOGO/free line: Finance's query always reports
        # -1 instead of 0 -- confirmed live 2026-09-02 across 369 lines
        # with zero variation. Corrected only in our own math.
        taxable_value = float(r.get("Taxable Value") or 0)
        discount_value = float(r.get("Discount Value") or 0)
        if ntv == -1 and taxable_value > 0 and abs(taxable_value - discount_value) < 0.01:
            ntv = 0.0
        bucket = out.setdefault(de, {"document": r.get("Document"), "ntv_sum": 0.0})
        bucket["ntv_sum"] = round(bucket["ntv_sum"] + ntv, 2)
    return out


# ── Postgres side ───────────────────────────────────────────────────────────

def _fetch_invoices(period_from: str, period_to: str) -> list[dict]:
    from unicommerce import store

    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.uniware_id, o.channel_name, o.order_date,
                   o.uc_taxable_value, o.uc_shipping,
                   o.sap_ar_invoice_docentry,
                   ship_line.shipping_tax_percent, ship_line.item_tax_percent
            FROM unicommerce_orders o
            LEFT JOIN LATERAL (
                SELECT
                    NULLIF(ol.shippingchargetaxpercentage, '')::numeric AS shipping_tax_percent,
                    GREATEST(
                        COALESCE(NULLIF(ol.integratedgstpercentage, '')::numeric, 0),
                        COALESCE(NULLIF(ol.state_gst_tax_percentage, '')::numeric, 0)
                          + COALESCE(NULLIF(ol.central_gst_tax_percentage, '')::numeric, 0)
                    ) AS item_tax_percent
                FROM unicommerce_order_lines ol
                WHERE ol.uniware_id = o.uniware_id
                  AND COALESCE(NULLIF(ol.shippingcharges, '')::numeric, 0) > 0
                ORDER BY ol.id
                LIMIT 1
            ) ship_line ON true
            WHERE o.order_date BETWEEN %s::date AND %s::date
              AND o.sap_ar_invoice_docentry IS NOT NULL
            ORDER BY o.channel_name, o.order_date, o.uniware_id
            """,
            (period_from, period_to),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_standard_cns(period_from: str, period_to: str) -> list[dict]:
    from unicommerce import store

    with store.connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT cn.uc_return_id, cn.uc_order_id, cn.return_date, cn.sap_cn_docentry,
                   o.channel_name
            FROM unicommerce_cn_staging cn
            LEFT JOIN unicommerce_orders o ON o.uniware_id = cn.uc_order_id
            WHERE cn.return_date BETWEEN %s::date AND %s::date
              AND cn.sap_cn_docentry IS NOT NULL
            ORDER BY cn.return_date, cn.uc_return_id
            """,
            (period_from, period_to),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _fetch_flex_cns(period_from: str, period_to: str) -> list[dict]:
    from unicommerce import store
    import psycopg

    with store.connect() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT refund_id, amazon_order_id, posted_date, doc_entry, payload_json
                FROM unicommerce_flex_cn_log
                WHERE posted_date BETWEEN %s::date AND %s::date
                  AND doc_entry IS NOT NULL
                ORDER BY posted_date, refund_id
                """,
                (period_from, period_to),
            )
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            return []
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _invoice_api_total(r: dict) -> float:
    """uc_taxable_value + shipping's own taxable (pre-tax) portion.

    Mirrors sap_payload._resolve_shipping_tax_percent + compute_tax_from_line
    exactly: shipping's own declared tax% if it's actually > 0, else the
    item's own tax rate (some channels, e.g. AMAZON_IN_API, store an
    explicit 0 on shipping's own field, not null -- falls through to the
    item rate same as the real posting code does).
    """
    shipping_gross = float(r["uc_shipping"] or 0)
    shipping_own_pct = float(r.get("shipping_tax_percent") or 0)
    shipping_tax_pct = shipping_own_pct if shipping_own_pct > 0 else float(r.get("item_tax_percent") or 0)
    shipping_taxable = shipping_gross / (1 + shipping_tax_pct / 100) if shipping_tax_pct > 0 else shipping_gross
    return float(r["uc_taxable_value"] or 0) + shipping_taxable


def _status_for(api_total: float | None, doc_entry, sap_by_entry: dict[int, dict]) -> tuple[str, float | None]:
    if not doc_entry:
        return "not_posted", None
    try:
        de = int(doc_entry)
    except (TypeError, ValueError):
        return "not_posted", None
    bucket = sap_by_entry.get(de)
    if bucket is None:
        return "posted_no_sap_line_data", None
    sap_total = bucket["ntv_sum"]
    if api_total is None:
        return "posted_confirmed_in_sap", sap_total
    # Credit memos: Finance records these as negative revenue; api_total
    # is always a positive magnitude. Compare magnitudes.
    if abs(api_total - abs(sap_total)) <= _AMOUNT_TOLERANCE:
        return "matched", sap_total
    return "amount_mismatch", sap_total


def fetch_monthly_summary(start_date: str, end_date: str) -> dict[str, Any]:
    """Channel-level Invoice/CreditNote reconciliation summary for
    [start_date, end_date] (order_date/return_date/posted_date scoped).
    Posted-only: a row with no SAP DocEntry yet is never included --
    this answers "does what we've posted match SAP," not "what's still
    outstanding" (unicommerce_orders' recon-gaps / dashboard stats
    already cover posting-pipeline status).

    Returns {"by_channel": [{channel, type, status, count, api_total,
    sap_total, diff}, ...], "grand_total": {...}, "period": {...}}.
    """
    from unicommerce import sap_payload

    card_codes = _card_codes()
    invoices = _fetch_invoices(start_date, end_date)
    standard_cns = _fetch_standard_cns(start_date, end_date)
    flex_cns = _fetch_flex_cns(start_date, end_date)
    finance_rows = _fetch_finance_revenue_rows(start_date, end_date, card_codes)
    sap_by_entry = _sap_totals_by_doc_entry(finance_rows)

    agg: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"count": 0, "api": 0.0, "sap": 0.0, "diff": 0.0})

    def _add(rtype: str, channel: str, status: str, api_total, sap_total, diff):
        bucket = agg[(rtype, channel or "UNKNOWN", status)]
        bucket["count"] += 1
        bucket["api"] += float(api_total) if api_total not in (None, "") else 0.0
        bucket["sap"] += float(sap_total) if sap_total not in (None, "") else 0.0
        bucket["diff"] += float(diff) if diff not in (None, "") else 0.0

    for r in invoices:
        api_total = _invoice_api_total(r)
        status, sap_total = _status_for(api_total, r.get("sap_ar_invoice_docentry"), sap_by_entry)
        if status == "not_posted":
            continue
        diff = round(api_total - sap_total, 2) if sap_total is not None else 0.0
        _add("Invoice", r.get("channel_name"), status, api_total, sap_total, diff)

    for r in standard_cns:
        status, sap_total = _status_for(None, r.get("sap_cn_docentry"), sap_by_entry)
        if status == "not_posted":
            continue
        _add("CreditNote-Standard", r.get("channel_name"), status, None, sap_total, 0.0)

    for r in flex_cns:
        payload = r.get("payload_json") or {}
        api_total = sap_payload.sum_unit_price_total(payload.get("DocumentLines") or []) if payload else 0.0
        status, sap_total = _status_for(api_total, r.get("doc_entry"), sap_by_entry)
        if status == "not_posted":
            continue
        diff = round(api_total - abs(sap_total), 2) if sap_total is not None else 0.0
        _add("CreditNote-Flex", "AMAZON_FLEX", status, api_total, sap_total, diff)

    by_channel = []
    grand = {"count": 0, "api_total": 0.0, "sap_total": 0.0, "diff": 0.0}
    for (rtype, channel, status), v in sorted(agg.items()):
        by_channel.append({
            "type": rtype, "channel": channel, "status": status,
            "count": v["count"], "api_total": round(v["api"], 2),
            "sap_total": round(v["sap"], 2), "diff": round(v["diff"], 2),
        })
        grand["count"] += v["count"]
        grand["api_total"] += v["api"]
        grand["sap_total"] += v["sap"]
        grand["diff"] += v["diff"]

    return {
        "period": {"start_date": start_date, "end_date": end_date},
        "by_channel": by_channel,
        "grand_total": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in grand.items()},
    }

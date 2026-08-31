"""HANA read for B2B fetch: OCRD/CRD1 (BP) and OITM (items).

Primary reads use HANA. Item tax UDFs fall back to Service Layer GET Items
(same as legacy Frappe Blink.py) when HANA misses a row or U_TaxRate is empty.
Order create/update stays on Service Layer (b2b_sappost.py).
Uses /sap/hana_config + /sap/hana_credentials (HANA_USE_SSM=1).
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

from khanal_shared.hana import get_hana_params, hana_connection, qualified_table

logger = logging.getLogger(__name__)


class SapError(Exception):
    pass


def _schema() -> str | None:
    params = get_hana_params()
    return params.schema if params else None


def _rows(cur) -> list[dict[str, Any]]:
    cols = [c[0] for c in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_business_partner(card_code: str) -> dict[str, Any]:
    """OCRD + CRD1 shaped like Service Layer BP + BPAddresses."""
    code = (card_code or "").strip()
    if not code:
        raise SapError("CardCode is empty.")
    if not get_hana_params():
        raise SapError("HANA is not configured (set HANA_USE_SSM=1 and /sap/hana_config).")

    ocrd = qualified_table(_schema(), "OCRD", "T0")
    crd1 = qualified_table(_schema(), "CRD1", "T1")
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT
              T0."CardCode",
              T0."CardName",
              T0."LicTradNum" AS "FederalTaxID",
              T0."LicTradNum" AS "GSTIN"
            FROM {ocrd}
            WHERE T0."CardCode" = ?
            """,
            (code,),
        )
        header_rows = _rows(cur)
        if not header_rows:
            raise SapError(f"No customer data found for CardCode: {code}.")
        bp = header_rows[0]
        cur.execute(
            f"""
            SELECT
              T1."Address"   AS "AddressName",
              T1."AdresType" AS "AddressType",
              T1."Street",
              T1."Block",
              T1."City",
              T1."State",
              T1."ZipCode",
              T1."Country",
              T1."GSTRegnNo" AS "GSTIN"
            FROM {crd1}
            WHERE T1."CardCode" = ?
            """,
            (code,),
        )
        addresses = _rows(cur)

    bp_gst = str(bp.get("GSTIN") or bp.get("FederalTaxID") or "").strip()
    for row in addresses:
        gst = str(row.get("GSTIN") or "").strip() or bp_gst
        row["GSTIN"] = gst
        row["FederalTaxID"] = gst
    bp["BPAddresses"] = addresses
    return bp


def _row_has_usable_tax(row: dict[str, Any]) -> bool:
    if not row.get("ItemCode"):
        return False
    raw = row.get("U_TaxRate")
    if raw is not None and raw != "" and str(raw).strip().upper() != "N/A":
        try:
            int(float(raw))
            return True
        except (TypeError, ValueError):
            pass
    for key in ("U_IgstTax", "U_GstTax"):
        text = str(row.get(key) or "").strip()
        if not text:
            continue
        if text.lower() in {"nil", "n/a", "na", "-"}:
            return True
        if re.search(r"\d+", text):
            return True
    return False


def _get_items_via_service_layer(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Legacy Frappe path: GET Items('{ItemCode}') for U_TaxRate / GST UDFs."""
    from khanal_shared.sap_sl import build_opener_and_base_via_ssm, sap_sl_request_json

    if not codes:
        return {}
    opener, base = build_opener_and_base_via_ssm()
    select = "ItemCode,ItemName,U_TaxRate,U_GstTax,U_IgstTax"
    found: dict[str, dict[str, Any]] = {}
    chunk_size = 15
    for i in range(0, len(codes), chunk_size):
        chunk = codes[i : i + chunk_size]
        filters = " or ".join(f"ItemCode eq '{c.replace(chr(39), chr(39) + chr(39))}'" for c in chunk)
        path = quote(
            f"Items?$filter=({filters})&$select={select}&$top={len(chunk)}",
            safe="()'=$?,",
        )
        status, body = sap_sl_request_json(opener, "GET", f"{base}/{path}")
        if status != 200 or not isinstance(body, dict):
            logger.warning("Service Layer Items lookup failed: HTTP %s for %s", status, chunk)
            continue
        for row in body.get("value") or []:
            code = str(row.get("ItemCode") or "").strip()
            if code:
                found[code] = row
    return found


def get_items(sap_codes: list[str]) -> dict[str, dict[str, Any]]:
    """OITM rows keyed by ItemCode. HANA first; Service Layer fallback for missing tax."""
    codes = [str(c).strip() for c in sap_codes if str(c).strip()]
    if not codes:
        return {}
    found: dict[str, dict[str, Any]] = {}
    if get_hana_params():
        oitm = qualified_table(_schema(), "OITM", "T0")
        placeholders = ",".join("?" * len(codes))
        sql = f"""
            SELECT
              T0."ItemCode",
              T0."ItemName",
              T0."U_TaxRate",
              T0."U_GstTax",
              T0."U_IgstTax"
            FROM {oitm}
            WHERE T0."ItemCode" IN ({placeholders})
        """
        try:
            with hana_connection() as conn:
                cur = conn.cursor()
                cur.execute(sql, tuple(codes))
                rows = _rows(cur)
            found = {str(r.get("ItemCode") or ""): r for r in rows if r.get("ItemCode")}
        except Exception:
            logger.exception("HANA OITM lookup failed; will try Service Layer fallback")
    else:
        logger.warning("HANA not configured; using Service Layer for OITM item lookup")

    need_sl = [c for c in codes if not _row_has_usable_tax(found.get(c) or {})]
    if need_sl:
        try:
            sl_found = _get_items_via_service_layer(need_sl)
            for code, row in sl_found.items():
                if _row_has_usable_tax(row):
                    found[code] = row
                elif code not in found:
                    found[code] = row
            if sl_found:
                logger.info("Service Layer filled OITM tax for %s", sorted(sl_found.keys()))
        except Exception:
            logger.exception("Service Layer OITM fallback failed for %s", need_sl)

    if not found and not get_hana_params():
        raise SapError("HANA is not configured and Service Layer item lookup failed.")
    return found


def get_item(sap_code: str) -> dict[str, Any] | None:
    found = get_items([sap_code])
    return found.get((sap_code or "").strip())

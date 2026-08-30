"""HANA read for Blinkit fetch: OCRD/CRD1 (BP) and OITM (items).

Replaces Service Layer GET BusinessPartners / GET Items.
Order create/update stays on Service Layer.
Uses /sap/hana_config + /sap/hana_credentials (HANA_USE_SSM=1).
"""

from __future__ import annotations

import logging
from typing import Any

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


def get_items(sap_codes: list[str]) -> dict[str, dict[str, Any]]:
    """OITM rows keyed by ItemCode. One round-trip for the PO."""
    codes = [str(c).strip() for c in sap_codes if str(c).strip()]
    if not codes:
        return {}
    if not get_hana_params():
        raise SapError("HANA is not configured (set HANA_USE_SSM=1 and /sap/hana_config).")

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
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(codes))
        rows = _rows(cur)
    return {str(r.get("ItemCode") or ""): r for r in rows if r.get("ItemCode")}


def get_item(sap_code: str) -> dict[str, Any] | None:
    found = get_items([sap_code])
    return found.get((sap_code or "").strip())

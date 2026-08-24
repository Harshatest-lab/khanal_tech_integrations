"""SAP Service Layer StockTransfers polling, for PoGrn + InventorySync.

Replaces the Frappe source's "SAP Inventory Transfers" doctype (which
doesn't exist in this Lambda architecture) -- polls SAP directly by
DocEntry watermark instead. List queries don't include BatchNumbers on
StockTransferLines (confirmed live against india_test), so list_since()
only returns header/line summaries; detail() fetches the full document
for the authoritative batch/qty breakdown both flows need.

DocEntry is a strictly increasing SAP sequence, so the watermark is a
plain integer comparison -- no overlap-window/timestamp-drift logic
needed, unlike fetch_sync.py's UC-side date cursor.
"""

from __future__ import annotations

from urllib.parse import quote

from unicommerce import sap_client


def list_since(since_doc_entry: int, limit: int = 50, doc_date_floor: str | None = None) -> list[dict]:
    """Transfers with DocEntry > since_doc_entry, ascending. Header + line
    summary only (no BatchNumbers) -- call detail() per DocEntry for that.

    doc_date_floor (see constants.POGRN_DOC_DATE_FLOOR) additionally
    excludes anything dated before it -- the watermark alone doesn't stop
    a fresh/reset scan from walking SAP's entire StockTransfers history."""
    doc_filter = f"DocEntry gt {int(since_doc_entry)}"
    if doc_date_floor:
        doc_filter += f" and DocDate ge '{doc_date_floor}'"
    status, body = sap_client._request(
        "GET",
        quote(
            f"StockTransfers?$filter={doc_filter}"
            f"&$orderby=DocEntry asc&$top={int(limit)}",
            safe="/?$&=',",
        ),
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"StockTransfers list failed (since={since_doc_entry}): HTTP {status}")
    return body.get("value", [])


def detail(doc_entry: int) -> dict:
    """Full document, including StockTransferLines[].BatchNumbers."""
    status, body = sap_client._request("GET", f"StockTransfers({int(doc_entry)})")
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"StockTransfers detail failed for DocEntry={doc_entry}: HTTP {status}")
    return body


def inward_lines(transfer: dict, watched_warehouses: set[str]) -> list[dict]:
    """Lines whose destination (WarehouseCode) is in the watched set --
    e.g. EC-FG or any AMZ-* warehouse."""
    return [
        line
        for line in (transfer.get("StockTransferLines") or [])
        if str(line.get("WarehouseCode") or "").strip() in watched_warehouses
    ]


def outward_lines(
    transfer: dict,
    amz_warehouses: set[str],
    ec_fg_from: str = "EC-FG",
    ec_fg_to: str = "EC-B2C",
) -> list[dict]:
    """Lines that are either EC-FG -> EC-B2C specifically, or FROM any
    AMZ-* warehouse to anywhere (confirmed with the user: AMZ-* outward
    has no specific destination pair like EC-FG does)."""
    out: list[dict] = []
    for line in transfer.get("StockTransferLines") or []:
        frm = str(line.get("FromWarehouseCode") or "").strip()
        to = str(line.get("WarehouseCode") or "").strip()
        if frm == ec_fg_from and to == ec_fg_to:
            out.append(line)
        elif frm in amz_warehouses:
            out.append(line)
    return out

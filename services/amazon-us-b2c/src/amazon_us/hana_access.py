"""Read-only HANA queries: FEFO batch allocation, GL account resolution,
and the SAP item-master SKU map.

Uses ``khanal_shared.hana`` (pooled hdbcli, profile via HANA_PROFILE ->
/sap/inc/hana_*; the pool SETs the configured schema on checkout, so
table names are unqualified here).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from khanal_shared.hana.pool import hana_connection

logger = logging.getLogger(__name__)


class HanaUnavailableError(Exception):
    """HANA unreachable. Orders must be left READY (never FAILED) so the
    next run retries them."""


class InsufficientStockError(Exception):
    """HANA reachable but FEFO stock cannot cover the requested quantity."""


@dataclass
class BatchAllocation:
    batch_number: str
    quantity: float

    def as_sap(self) -> dict:
        return {"BatchNumber": self.batch_number, "Quantity": self.quantity}


def allocate_from_rows(
    rows: list[tuple], required_qty: float, item_code: str, warehouse: str
) -> list[BatchAllocation]:
    """Pure FEFO fill: rows are (batch_number, available_qty), FEFO-ordered.

    Empty rows -> [] (item not batch-managed; caller omits BatchNumbers).
    Raises InsufficientStockError when rows exist but cannot cover the qty.
    """
    if not rows:
        return []

    allocated: list[BatchAllocation] = []
    remaining = float(required_qty)

    for batch_no, avail in rows:
        if remaining <= 0:
            break
        avail = float(avail or 0)
        if avail <= 0:
            continue
        take = min(avail, remaining)
        allocated.append(BatchAllocation(batch_number=str(batch_no).strip(), quantity=take))
        remaining -= take

    if round(remaining, 3) > 0:
        total = sum(float(a or 0) for _, a in rows)
        raise InsufficientStockError(
            f"Insufficient stock for {item_code} in {warehouse}: "
            f"need {required_qty}, available {total:.2f}"
        )
    return allocated


def allocate_batches_fefo(item_code: str, required_qty: float, warehouse: str) -> list[dict]:
    """FEFO allocation as SAP BatchNumbers dicts.

    Raises HanaUnavailableError (leave order READY) or
    InsufficientStockError (mark order FAILED)."""
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                '''
                SELECT T1."DistNumber", T0."Quantity"
                FROM "OBTQ" T0
                JOIN "OBTN" T1
                    ON T0."ItemCode" = T1."ItemCode"
                   AND T0."SysNumber" = T1."SysNumber"
                WHERE T0."ItemCode" = ? AND T0."WhsCode" = ? AND T0."Quantity" > 0
                ORDER BY T1."ExpDate" ASC NULLS LAST, T1."InDate" ASC
                ''',
                [item_code, warehouse],
            )
            rows = cur.fetchall() or []
            cur.close()
    except InsufficientStockError:
        raise
    except Exception as e:
        raise HanaUnavailableError(f"FEFO query failed for {item_code}: {e}") from e

    allocations = allocate_from_rows(rows, required_qty, item_code, warehouse)
    return [a.as_sap() for a in allocations]


def get_bom_children(item_code: str) -> list[tuple[str, float]]:
    """(child_item_code, qty_per_parent_unit) for a Sales BOM (TreeType='S')
    parent — [] if item_code isn't a BOM (ITT1 has no rows for it).

    Ported from unicommerce-b2c's bundle-line pattern: a Sales BOM parent
    must be posted as a zero-price/zero-batch "iSalesTree" line followed by
    one real "iIngredient" line per component (own price, own FEFO batch
    allocation) — SAP explodes BOM parents into their components at
    document-add time and rejects the whole document if a batch-managed
    component has no batch selected. Sending merged batches on the
    parent's single line (what this pipeline first tried) doesn't work:
    confirmed against SAP Community reports of the same DI API/Service
    Layer limitation.
    """
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            # Only TreeType='S' (Sales BOM) explodes at the sales-document
            # level. A great many otherwise-ordinary items also carry a
            # Production BOM (TreeType='P') documenting how THEY get
            # manufactured from raw materials — that's manufacturing/costing
            # data, not something to substitute into an AR Invoice. Without
            # this filter, any plain finished-good item that happens to have
            # a production recipe gets wrongly exploded into its raw
            # materials, which typically aren't even flagged as sellable.
            cur.execute('SELECT "TreeType" FROM "OITM" WHERE "ItemCode" = ?', [item_code])
            row = cur.fetchone()
            if not row or row[0] != "S":
                cur.close()
                return []
            cur.execute('SELECT "Code", "Quantity" FROM "ITT1" WHERE "Father" = ?', [item_code])
            rows = cur.fetchall() or []
            cur.close()
    except Exception as e:
        raise HanaUnavailableError(f"BOM children query failed for {item_code}: {e}") from e
    return [(str(code).strip(), float(qty or 0)) for code, qty in rows]


_acct_cache: dict[str, str] = {}


def resolve_acct_code(format_code: str) -> str:
    """OACT.AcctCode for a GL FormatCode (Service Layer needs the internal
    code, e.g. _SYS00000000059). Falls back to the input on any failure;
    cached in-process (GL accounts do not move)."""
    if not format_code:
        return format_code
    if format_code in _acct_cache:
        return _acct_cache[format_code]
    stripped = format_code.replace("-", "")
    try:
        with hana_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT "AcctCode" FROM "OACT" WHERE "FormatCode" = ?', [stripped])
            row = cur.fetchone()
            cur.close()
    except Exception as e:
        logger.error("resolve_acct_code failed for %s: %s", format_code, e)
        return format_code
    resolved = str(row[0]).strip() if row else format_code
    _acct_cache[format_code] = resolved
    return resolved


def fetch_amazon_sku_map() -> dict[str, list[str]]:
    """{seller_sku: [item_code, ...]} from OITM.U_Amz_SKU.

    Control characters are stripped BEFORE grouping: 94 of 118 production
    rows once carried a leading \\r (Excel-paste artifact) that displays
    fine in the SAP UI but breaks exact-match lookups entirely.
    """
    with hana_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            '''
            SELECT "U_Amz_SKU", "ItemCode"
            FROM "OITM"
            WHERE "U_Amz_SKU" IS NOT NULL AND LENGTH(TRIM("U_Amz_SKU")) > 0
            '''
        )
        rows = cur.fetchall() or []
        cur.close()

    sku_map: dict[str, list[str]] = {}
    for raw_sku, item_code in rows:
        sku = clean_sku(raw_sku)
        if not sku:
            continue
        sku_map.setdefault(sku, []).append(str(item_code).strip())
    return sku_map


def clean_sku(raw: object) -> str:
    """Strip control characters + surrounding whitespace from a SKU value."""
    return "".join(ch for ch in str(raw or "") if ord(ch) >= 32).strip()

"""Amazon Flex (PRCJ) SKU resolution: Amazon seller SKU -> SAP FG item code.

The design doc's original plan was to port khanal-oms's sku_resolver.py +
its channel_item_map table. Checked instead (2026-08-20) and found that
table has 1 row total -- khanal-oms is still pre-launch (DRY_RUN=true), so
there's effectively no real mapping data behind it yet, in either service.

Flex ships the exact same seller catalog as the already-integrated Amazon
FBA/Easy Ship channels (same Amazon seller account) -- confirmed against
unicommerce-b2c's own order history: unicommerce_order_lines already holds
1380 real, UC-resolved (sellerskucode -> itemsku) pairs across
AMAZON_FBA_IN_* + AMAZON_IN_API, cleanly 1:1 (checked, zero seller SKUs map
to more than one item code). Cross-checked against a real 7-day PRCJ
shipments pull (2026-08-20): 21/43 distinct PRCJ seller SKUs already
resolved via this history with zero new integration. The remainder are
seller SKUs that have simply never appeared in an already-synced order
(new listings) -- those need the manual override tier below, same as any
newly-listed product would need regardless of source.

CFG (Sales BOM) bundle expansion: initially deferred (checked
unicommerce_order_lines.itemsku across this service's entire order history,
every channel -- zero rows had ever resolved to a CFG-prefixed SAP item
code). Revisited 2026-08-20 against a real Channel Item Type Report export
from Unicommerce: 31 of 138 real seller-SKU mappings are CFG parents, and
15 of 43 real PRCJ SKUs in a 7-day sample resolve to one -- ~35% of real
Flex volume, not rare. Confirmed directly with the user: CFG codes are
real non-inventory SAP items with child items looked up via SAP
ProductTrees (same iSalesTree mechanism the MTR script and this service's
own sap_payload.py bundle-grouping already use); batch numbers belong on
the children, never the parent -- sap_payload.py's existing
bundleSkuCode-grouping already guarantees this (parent line is hardcoded
BatchNumbers: [], children go through the normal per-line FEFO path).
resolve_seller_sku returns the CFG parent code as-is; the caller
(flex_fetch.py) is responsible for calling get_product_tree_children and
expanding into per-child rows tagged with bundleSkuCode.
"""

from __future__ import annotations

import psycopg

from unicommerce import sap_client
from unicommerce.settings import UnicommerceSettings


class SkuUnresolved(Exception):
    """No override and no live-order-history match for this Amazon seller
    SKU. Never guess -- callers must flag/skip the line, not post against
    the wrong item."""


class CfgExpansionFailed(Exception):
    """SAP ProductTree lookup for a CFG parent failed or returned no
    children -- callers must flag/skip the line, not guess at what the
    bundle contains."""


def build_sku_map(conn: psycopg.Connection) -> dict[str, str]:
    """{seller SKU: SAP FG item code}, sourced from unicommerce-b2c's own
    already-synced Amazon order history. Load once per fetch run and pass
    to resolve_seller_sku for every line -- same "load once, resolve many"
    convention as geo.py/gst.py, not a per-line query."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT l.sellerskucode, l.itemsku
            FROM unicommerce_order_lines l
            JOIN unicommerce_orders o ON o.uniware_id = l.uniware_id
            WHERE o.channel_name ILIKE 'AMAZON%%'
              AND l.sellerskucode != '' AND l.itemsku != ''
            """
        )
        rows = cur.fetchall()
    return {seller_sku: item_sku for seller_sku, item_sku in rows}


def is_cfg_parent(item_code: str) -> bool:
    """SAP Sales-BOM parent naming convention, per the MTR script's
    _is_cfg_parent_item."""
    return (item_code or "").strip().upper().startswith("CFG")


def resolve_seller_sku(sku_map: dict[str, str], settings: UnicommerceSettings, seller_sku: str) -> str:
    """Manual override tier (settings.flex_sku_overrides) wins over the
    live-order-history map -- same override-wins precedence as
    khanal-oms's sku_resolver.py. Raises SkuUnresolved if neither covers
    it. Never guesses. Returns the resolved code as-is, whether plain or a
    CFG parent -- callers must check is_cfg_parent() and expand via
    get_product_tree_children() themselves; this function only resolves
    the seller SKU to a SAP item code."""
    key = (seller_sku or "").strip()
    if not key:
        raise SkuUnresolved("empty seller SKU")

    item_code = settings.flex_sku_overrides.get(key) or sku_map.get(key)
    if not item_code:
        raise SkuUnresolved(f"no override and no live-order-history match for seller SKU {key!r}")

    return item_code


def get_product_tree_children(parent_item_code: str) -> list[dict]:
    """[{"item_code": str, "quantity": float}, ...] for a CFG (Sales BOM)
    parent's child items, via SAP ProductTrees. Ported from the MTR
    script's _get_product_tree_children (same 3-endpoint fallback for SAP
    Service Layer version differences, same per-run cache). Raises
    CfgExpansionFailed if every endpoint variant fails or returns no
    children -- never guesses at bundle contents."""
    try:
        return sap_client.get_product_tree_children(parent_item_code)
    except Exception as e:
        raise CfgExpansionFailed(f"ProductTree lookup failed for CFG parent {parent_item_code!r}: {e}") from e

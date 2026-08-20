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

CFG (Sales BOM) bundle expansion is deliberately NOT ported here: checked
unicommerce_order_lines.itemsku across this service's ENTIRE order history,
every channel, ever -- zero rows have ever resolved to a CFG-prefixed SAP
item code. ProductTree expansion (khanal_tech_integrations MTR script's
_get_product_tree_children) is real, proven code, but porting it against a
code path that's never actually fired would be guessing at whether the port
is even correct. If a Flex order ever resolves to a real CFG parent,
resolve_seller_sku raises CfgExpansionNotImplemented instead of silently
mis-posting or guessing at pricing splits -- port the expansion then,
against a real example.
"""

from __future__ import annotations

import psycopg

from unicommerce.settings import UnicommerceSettings


class SkuUnresolved(Exception):
    """No override and no live-order-history match for this Amazon seller
    SKU. Never guess -- callers must flag/skip the line, not post against
    the wrong item."""


class CfgExpansionNotImplemented(Exception):
    """A Flex line resolved to a real SAP CFG (Sales BOM) parent -- the
    first time this has happened across this service's history. Port
    ProductTree expansion now, against this real example, rather than the
    speculative version that was deliberately left unbuilt."""


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
    _is_cfg_parent_item -- kept for when CfgExpansionNotImplemented needs
    porting for real."""
    return (item_code or "").strip().upper().startswith("CFG")


def resolve_seller_sku(sku_map: dict[str, str], settings: UnicommerceSettings, seller_sku: str) -> str:
    """Manual override tier (settings.flex_sku_overrides) wins over the
    live-order-history map -- same override-wins precedence as
    khanal-oms's sku_resolver.py. Raises SkuUnresolved if neither covers
    it, or CfgExpansionNotImplemented if the resolved code is a CFG
    parent. Never guesses."""
    key = (seller_sku or "").strip()
    if not key:
        raise SkuUnresolved("empty seller SKU")

    item_code = settings.flex_sku_overrides.get(key) or sku_map.get(key)
    if not item_code:
        raise SkuUnresolved(f"no override and no live-order-history match for seller SKU {key!r}")

    if is_cfg_parent(item_code):
        raise CfgExpansionNotImplemented(
            f"seller SKU {key!r} resolved to CFG parent {item_code!r} -- ProductTree expansion not yet ported"
        )

    return item_code

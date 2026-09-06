"""Per-user access levels for dashboard resources."""

from __future__ import annotations

from typing import Any

PERM_NONE = "none"
PERM_VIEW = "view"
PERM_EDIT = "edit"
PERM_DELETE = "delete"

RESOURCE_ORDERS = "orders"
RESOURCE_SKU_MAP = "sku_map"
RESOURCE_SHIP_TO = "ship_to"
RESOURCE_DELIVERY = "delivery"

ALL_RESOURCES = (RESOURCE_ORDERS, RESOURCE_SKU_MAP, RESOURCE_SHIP_TO, RESOURCE_DELIVERY)

_LEVEL_RANK = {PERM_NONE: 0, PERM_VIEW: 1, PERM_EDIT: 2, PERM_DELETE: 3}

_DEFAULTS_BY_ROLE = {
    "admin": {
        RESOURCE_ORDERS: PERM_DELETE,
        RESOURCE_SKU_MAP: PERM_DELETE,
        RESOURCE_SHIP_TO: PERM_DELETE,
        RESOURCE_DELIVERY: PERM_DELETE,
    },
    "reviewer": {
        RESOURCE_ORDERS: PERM_EDIT,
        RESOURCE_SKU_MAP: PERM_EDIT,
        RESOURCE_SHIP_TO: PERM_EDIT,
        RESOURCE_DELIVERY: PERM_EDIT,
    },
    "viewer": {
        RESOURCE_ORDERS: PERM_VIEW,
        RESOURCE_SKU_MAP: PERM_VIEW,
        RESOURCE_SHIP_TO: PERM_VIEW,
        RESOURCE_DELIVERY: PERM_NONE,
    },
}


def normalize_level(raw: Any) -> str:
    level = str(raw or "").strip().lower()
    return level if level in _LEVEL_RANK else ""


def defaults_for_role(role: str) -> dict[str, str]:
    return dict(_DEFAULTS_BY_ROLE.get(str(role or "viewer"), _DEFAULTS_BY_ROLE["viewer"]))


def resolve_permissions(user: dict[str, Any]) -> dict[str, str]:
    role = str(user.get("role") or "viewer")
    defaults = defaults_for_role(role)
    if user.get("can_manage_users"):
        return {r: PERM_DELETE for r in ALL_RESOURCES}
    out: dict[str, str] = {}
    for resource in ALL_RESOURCES:
        col = f"perm_{resource}"
        explicit = normalize_level(user.get(col))
        out[resource] = explicit or defaults.get(resource, PERM_VIEW)
    return out


def has_perm(user: dict[str, Any], resource: str, need: str) -> bool:
    if user.get("can_manage_users"):
        return True
    resolved = resolve_permissions(user)
    have = _LEVEL_RANK.get(resolved.get(resource, PERM_NONE), 0)
    want = _LEVEL_RANK.get(need, 0)
    return have >= want


def attach_permissions(user: dict[str, Any]) -> dict[str, Any]:
    data = dict(user)
    data["permissions"] = resolve_permissions(data)
    return data

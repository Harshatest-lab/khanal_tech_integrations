"""Unit tests for per-user resource permissions."""

from permissions import (
    PERM_DELETE,
    PERM_EDIT,
    PERM_NONE,
    PERM_VIEW,
    RESOURCE_ORDERS,
    RESOURCE_SHIP_TO,
    RESOURCE_SKU_MAP,
    attach_permissions,
    defaults_for_role,
    has_perm,
    normalize_level,
    resolve_permissions,
)


def test_normalize_level():
    assert normalize_level("Edit") == "edit"
    assert normalize_level("bogus") == ""
    assert normalize_level(None) == ""


def test_role_defaults():
    assert defaults_for_role("reviewer")[RESOURCE_ORDERS] == PERM_EDIT
    assert defaults_for_role("viewer")[RESOURCE_SKU_MAP] == PERM_VIEW
    assert defaults_for_role("admin")[RESOURCE_SHIP_TO] == PERM_DELETE


def test_resolve_uses_explicit_override():
    user = {"role": "reviewer", "perm_orders": "view", "perm_sku_map": "", "perm_ship_to": "none"}
    perms = resolve_permissions(user)
    assert perms[RESOURCE_ORDERS] == PERM_VIEW
    assert perms[RESOURCE_SKU_MAP] == PERM_EDIT
    assert perms[RESOURCE_SHIP_TO] == PERM_NONE


def test_admin_manage_users_gets_full_access():
    user = {"role": "viewer", "can_manage_users": True, "perm_orders": "none"}
    assert resolve_permissions(user)[RESOURCE_ORDERS] == PERM_DELETE
    assert has_perm(user, RESOURCE_ORDERS, PERM_DELETE)


def test_has_perm_hierarchy():
    user = attach_permissions({"role": "reviewer", "perm_orders": "edit"})
    assert has_perm(user, RESOURCE_ORDERS, PERM_VIEW)
    assert has_perm(user, RESOURCE_ORDERS, PERM_EDIT)
    assert not has_perm(user, RESOURCE_ORDERS, PERM_DELETE)


def test_reviewer_without_delete_cannot_delete():
    user = attach_permissions({"role": "reviewer"})
    assert has_perm(user, RESOURCE_ORDERS, PERM_EDIT)
    assert not has_perm(user, RESOURCE_ORDERS, PERM_DELETE)


def test_attach_permissions_adds_dict():
    user = attach_permissions({"role": "viewer"})
    assert "permissions" in user
    assert user["permissions"][RESOURCE_ORDERS] == PERM_VIEW

"""Unit tests for per-user resource permissions (delivery)."""

from permissions import (
    PERM_DELETE,
    PERM_NONE,
    RESOURCE_DELIVERY,
    attach_permissions,
    defaults_for_role,
    has_perm,
    resolve_permissions,
)


def test_admin_defaults_include_delivery_delete():
    assert defaults_for_role("admin")[RESOURCE_DELIVERY] == PERM_DELETE


def test_viewer_defaults_no_delivery():
    assert defaults_for_role("viewer")[RESOURCE_DELIVERY] == PERM_NONE


def test_explicit_delivery_override():
    user = {"role": "viewer", "perm_delivery": "edit"}
    perms = resolve_permissions(user)
    assert perms[RESOURCE_DELIVERY] == "edit"
    assert has_perm(attach_permissions(user), RESOURCE_DELIVERY, "edit")

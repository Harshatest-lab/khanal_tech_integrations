"""Tests for logistics portal auth helpers."""

from src.portal_auth import (
    GROUP_ADMIN,
    GROUP_DELIVERY,
    PERM_DELETE,
    PERM_EDIT,
    PERM_NONE,
    has_delivery_perm,
    resolve_delivery_level,
)


def test_cognito_delivery_group_gets_edit():
    level = resolve_delivery_level(None, [GROUP_DELIVERY])
    assert level == PERM_EDIT
    assert has_delivery_perm(level, PERM_EDIT)


def test_admin_group_gets_delete():
    level = resolve_delivery_level(None, [GROUP_ADMIN])
    assert level == PERM_DELETE


def test_db_user_explicit_delivery():
    user = {
        "role": "viewer",
        "is_active": True,
        "perm_delivery": "view",
        "can_manage_users": False,
        "channels": ["logistics"],
    }
    level = resolve_delivery_level(user, [])
    assert level == "view"
    assert not has_delivery_perm(level, PERM_EDIT)


def test_viewer_default_no_delivery():
    user = {
        "role": "viewer",
        "is_active": True,
        "perm_delivery": "",
        "can_manage_users": False,
        "channels": ["logistics"],
    }
    assert resolve_delivery_level(user, []) == PERM_NONE


def test_reviewer_without_logistics_channel_denied():
    user = {
        "role": "reviewer",
        "is_active": True,
        "perm_delivery": "",
        "can_manage_users": False,
        "channels": ["blinkit"],
    }
    assert resolve_delivery_level(user, []) == PERM_NONE


def test_reviewer_with_logistics_channel_gets_edit():
    user = {
        "role": "reviewer",
        "is_active": True,
        "perm_delivery": "",
        "can_manage_users": False,
        "channels": ["logistics"],
    }
    assert resolve_delivery_level(user, []) == PERM_EDIT


def test_cognito_delivery_group_bypasses_missing_channel():
    user = {
        "role": "viewer",
        "is_active": True,
        "perm_delivery": "",
        "can_manage_users": False,
        "channels": ["blinkit"],
    }
    assert resolve_delivery_level(user, [GROUP_DELIVERY]) == PERM_EDIT

from __future__ import annotations

import sys
from pathlib import Path

_B2B_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _B2B_SRC in sys.path:
    sys.path.remove(_B2B_SRC)
sys.path.insert(0, _B2B_SRC)
_handlers = sys.modules.get("handlers")
if _handlers is not None:
    mod_file = (getattr(_handlers, "__file__", "") or "").replace("\\", "/")
    if _B2B_SRC not in mod_file:
        sys.modules.pop("handlers", None)

from handlers import ROLE_ADMIN, ROLE_REVIEWER, ROLE_VIEWER, _can_admin, _can_write, _groups, _role


def _event(groups: str | list[str]):
    return {
        "requestContext": {
            "authorizer": {"jwt": {"claims": {"cognito:groups": groups, "email": "ops@test.com"}}}
        }
    }


def test_admin_can_write_and_admin():
    groups = _groups(_event("[B2BAdmin]"))
    assert ROLE_ADMIN in groups
    assert _can_admin(groups)
    assert _can_write(groups)
    assert _role(groups) == ROLE_ADMIN


def test_reviewer_can_write_not_admin():
    groups = _groups(_event("B2BReviewer"))
    assert _can_write(groups)
    assert not _can_admin(groups)
    assert _role(groups) == ROLE_REVIEWER


def test_viewer_is_default():
    groups = _groups(_event(""))
    assert _role(groups) == ROLE_VIEWER
    assert not _can_write(groups)

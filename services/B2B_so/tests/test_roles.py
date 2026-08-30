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

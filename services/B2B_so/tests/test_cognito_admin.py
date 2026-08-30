from cognito_admin import password_ok


def test_password_policy():
    assert password_ok("short") != ""
    assert password_ok("nouppercase1") != ""
    assert password_ok("NOLOWERCASE1") != ""
    assert password_ok("NoDigitsHere") != ""
    assert password_ok("Harsha@123") == ""

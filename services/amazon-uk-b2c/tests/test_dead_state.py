"""Attempt counting / dead state in the posting modules (no real DB: a fake
connection records the SQL parameters and returns what Postgres would)."""

from amazon_uk import post_credit_notes_to_sap, post_to_sap, store


class _Cur:
    def __init__(self, log, row):
        self.log, self.row = log, row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.log, self.row = [], row

    def cursor(self):
        return _Cur(self.log, self.row)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def test_max_attempts_is_five():
    assert store.MAX_POST_ATTEMPTS == 5


def test_invoice_failure_reports_attempts_and_dead(monkeypatch):
    conn = _Conn((5, True))
    monkeypatch.setattr(post_to_sap.store, "connect", lambda: conn)
    info = post_to_sap._record("111-1", "failed", error="boom")
    assert info == {"attempts": 5, "dead": True}
    params = conn.log[0][1]
    assert params["max"] == 5 and params["first"] == 1 and params["failed"] is True


def test_invoice_success_counts_no_attempt(monkeypatch):
    conn = _Conn((2, False))
    monkeypatch.setattr(post_to_sap.store, "connect", lambda: conn)
    assert post_to_sap._record("111-1", "posted", "9", "10") == {}
    assert conn.log[0][1]["first"] == 0 and conn.log[0][1]["failed"] is False


def test_credit_note_failure_reports_attempts_and_dead():
    conn = _Conn((3, False))
    info = post_credit_notes_to_sap._record(conn, "T1", "111-1", "failed", error="boom")
    assert info == {"attempts": 3, "dead": False}
    assert conn.log[0][1]["max"] == 5


def test_automated_selection_skips_dead():
    import inspect
    for fn in (post_to_sap._shipped_orders_not_posted, post_to_sap._shipped_orders_not_posted_all):
        assert "sp.dead" in inspect.getsource(fn)
    assert "NOT sp.dead" in inspect.getsource(post_to_sap._failed_order_ids)
    for fn in (post_credit_notes_to_sap._refunds_not_posted, post_credit_notes_to_sap._refunds_not_posted_all):
        assert "cn.dead" in inspect.getsource(fn)
    assert "NOT cn.dead" in inspect.getsource(post_credit_notes_to_sap.retry_failed_refunds)

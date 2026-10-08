import pytest

import handlers


@pytest.fixture()
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(handlers, "send_alert", lambda subject, body: sent.append((subject, body)))
    return sent


# ── fetch_handler ──────────────────────────────────────────────────────────

def test_fetch_default_is_incremental(monkeypatch, alerts):
    monkeypatch.setattr(handlers.sync_amazonuk, "sync_incremental", lambda lb=None: {"lookback": lb})
    assert handlers.fetch_handler({}, None) == {"lookback": None}
    assert handlers.fetch_handler(None, None) == {"lookback": None}


def test_fetch_incremental_passes_initial_lookback(monkeypatch, alerts):
    monkeypatch.setattr(handlers.sync_amazonuk, "sync_incremental", lambda lb=None: {"lookback": lb})
    result = handlers.fetch_handler({"mode": "incremental", "initial_lookback_days": "1"}, None)
    assert result == {"lookback": 1}


def test_fetch_backfill_passes_dates(monkeypatch, alerts):
    monkeypatch.setattr(handlers.sync_amazonuk, "sync", lambda a, b: {"range": (a, b)})
    result = handlers.fetch_handler(
        {"mode": "backfill", "from_date": "2026-08-01", "to_date": "2026-08-31"}, None
    )
    assert result == {"range": ("2026-08-01", "2026-08-31")}


def test_fetch_backfill_requires_dates(alerts):
    with pytest.raises(ValueError, match="from_date and to_date"):
        handlers.fetch_handler({"mode": "backfill"}, None)


def test_fetch_unknown_mode_raises_and_alerts(alerts):
    with pytest.raises(ValueError, match="Unknown fetch mode"):
        handlers.fetch_handler({"mode": "nope"}, None)
    assert alerts and "fetch[nope] crashed" in alerts[0][0]


# ── post_invoices_handler ──────────────────────────────────────────────────

def test_post_default_posts_all_shipped(monkeypatch, alerts):
    monkeypatch.setattr(handlers.post_to_sap, "post_all_shipped_orders", lambda: {"called": "all"})
    assert handlers.post_invoices_handler({}, None) == {"called": "all"}
    assert handlers.post_invoices_handler({"mode": "post"}, None) == {"called": "all"}


def test_post_retry_mode(monkeypatch, alerts):
    monkeypatch.setattr(handlers.post_to_sap, "retry_failed_orders", lambda: {"called": "retry"})
    assert handlers.post_invoices_handler({"mode": "retry"}, None) == {"called": "retry"}


def test_post_single_order(monkeypatch, alerts):
    monkeypatch.setattr(handlers.post_to_sap, "post_single_order", lambda oid: {"called": oid})
    assert handlers.post_invoices_handler({"amazon_order_id": " 402-1 "}, None) == {"called": "402-1"}


def test_post_unknown_mode_raises(alerts):
    with pytest.raises(ValueError, match="Unknown post mode"):
        handlers.post_invoices_handler({"mode": "nope"}, None)


# ── fetch_refunds_handler ────────────────────────────────────────────────

def test_fetch_refunds_default_is_incremental(monkeypatch, alerts):
    monkeypatch.setattr(handlers.sync_refunds, "sync_incremental", lambda lb=None: {"lookback": lb})
    assert handlers.fetch_refunds_handler({}, None) == {"lookback": None}
    assert handlers.fetch_refunds_handler(None, None) == {"lookback": None}


def test_fetch_refunds_backfill_passes_dates(monkeypatch, alerts):
    monkeypatch.setattr(handlers.sync_refunds, "sync", lambda a, b: {"range": (a, b)})
    result = handlers.fetch_refunds_handler(
        {"mode": "backfill", "from_date": "2026-08-01", "to_date": "2026-08-31"}, None
    )
    assert result == {"range": ("2026-08-01", "2026-08-31")}


def test_fetch_refunds_backfill_requires_dates(alerts):
    with pytest.raises(ValueError, match="from_date and to_date"):
        handlers.fetch_refunds_handler({"mode": "backfill"}, None)


def test_fetch_refunds_unknown_mode_raises_and_alerts(alerts):
    with pytest.raises(ValueError, match="Unknown fetch mode"):
        handlers.fetch_refunds_handler({"mode": "nope"}, None)
    assert alerts and "fetch_refunds[nope] crashed" in alerts[0][0]


# ── post_credit_notes_handler ────────────────────────────────────────────

def test_post_credit_notes_default_posts_all(monkeypatch, alerts):
    monkeypatch.setattr(handlers.post_credit_notes_to_sap, "post_all_refunds", lambda: {"called": "all"})
    assert handlers.post_credit_notes_handler({}, None) == {"called": "all"}
    assert handlers.post_credit_notes_handler({"mode": "post"}, None) == {"called": "all"}


def test_post_credit_notes_retry_mode(monkeypatch, alerts):
    monkeypatch.setattr(handlers.post_credit_notes_to_sap, "retry_failed_refunds", lambda: {"called": "retry"})
    assert handlers.post_credit_notes_handler({"mode": "retry"}, None) == {"called": "retry"}


def test_post_single_refund(monkeypatch, alerts):
    monkeypatch.setattr(handlers.post_credit_notes_to_sap, "post_single_refund", lambda tid: {"called": tid})
    assert handlers.post_credit_notes_handler({"transaction_id": " txn-1 "}, None) == {"called": "txn-1"}


def test_post_credit_notes_unknown_mode_raises(alerts):
    with pytest.raises(ValueError, match="Unknown post mode"):
        handlers.post_credit_notes_handler({"mode": "nope"}, None)


# ── crash alerting ─────────────────────────────────────────────────────────

def test_crash_alert_scrubs_secrets_and_reraises(monkeypatch, alerts):
    monkeypatch.setenv("DB_PASSWORD", "hunter2-secret")

    def boom():
        raise RuntimeError("connect failed: password=hunter2-secret")

    monkeypatch.setattr(handlers.sync_amazonuk, "sync_incremental", lambda lb=None: boom())
    with pytest.raises(RuntimeError):
        handlers.fetch_handler({}, None)
    subject, body = alerts[0]
    assert "hunter2-secret" not in body and "***" in body

import json

import pytest

import handlers
from amazon_uk import alerts, dashboard


def test_unknown_route_404():
    status, body = dashboard.dispatch("GET", "/api/unknown")
    assert status == 404 and "not found" in body


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("path", [
    "/api/fetch", "/api/post-to-sap", "/api/retry-sap-failures", "/api/orders/402-1/post-to-sap", "/api/stats",
    "/api/orders/402-1234567-1234567/raw", "/api/orders",
    "/api/fetch-refunds", "/api/refunds/txn-1/post-to-sap", "/api/refunds",
])
def test_dashboard_is_read_only(method, path):
    status, body = dashboard.dispatch(method, path)
    assert status == 405 and "read-only" in body


def test_no_trigger_code_left_in_dashboard():
    # Regression guard: the dashboard must not be able to start jobs.
    # The ONLY allowed action is queueing one order (handle_post_order).
    for name in ("handle_fetch", "handle_post_to_sap", "handle_retry_sap_failures",
                 "handle_post_one_to_sap", "handle_fetch_refunds", "handle_post_one_refund_to_sap",
                 "_invoke_async"):
        assert not hasattr(dashboard, name), name


ORDER = "402-1234567-1234567"


def _patch_eligibility(monkeypatch, rows):
    monkeypatch.setattr(dashboard, "query", lambda sql, params=None: rows)


class _FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


def test_post_order_queues_async_invoke(monkeypatch):
    fake = _FakeLambda()
    monkeypatch.setattr(dashboard, "_lambda_client", lambda: fake)
    monkeypatch.setenv("POST_INVOICES_FUNCTION", "amazon-uk-b2c-prod-amazonUkPostInvoices")
    _patch_eligibility(monkeypatch, [{"shipped": True, "posted": None}])
    status, body = dashboard.dispatch("POST", f"/api/orders/{ORDER}/post")
    assert status == 202 and json.loads(body)["queued"] is True
    (call,) = fake.calls
    assert call["FunctionName"] == "amazon-uk-b2c-prod-amazonUkPostInvoices"
    assert call["InvocationType"] == "Event"
    assert json.loads(call["Payload"]) == {"amazon_order_id": ORDER}


@pytest.mark.parametrize("rows,expected", [
    ([], 404),
    ([{"shipped": True, "posted": True}], 409),
    ([{"shipped": False, "posted": None}], 409),
])
def test_post_order_rejects_ineligible(monkeypatch, rows, expected):
    fake = _FakeLambda()
    monkeypatch.setattr(dashboard, "_lambda_client", lambda: fake)
    monkeypatch.setenv("POST_INVOICES_FUNCTION", "fn")
    _patch_eligibility(monkeypatch, rows)
    status, _ = dashboard.dispatch("POST", f"/api/orders/{ORDER}/post")
    assert status == expected and not fake.calls


def test_post_order_rejects_bad_id_without_query(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not query")

    monkeypatch.setattr(dashboard, "query", boom)
    status, _ = dashboard.dispatch("POST", "/api/orders/1' OR 1=1--/post")
    assert status == 400


def test_post_order_get_not_routed():
    # GET on the post path must not fall through to anything that posts.
    status, _ = dashboard.dispatch("GET", f"/api/orders/{ORDER}/post")
    assert status == 404


def test_refunds_route_queries(monkeypatch):
    class FakeCursor:
        description = [("count",)]
        _calls = 0

        def execute(self, sql, params=None):
            FakeCursor._calls += 1

        def fetchall(self):
            return [(2,)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(dashboard.store, "connect", lambda: FakeConn())
    status, body = dashboard.dispatch("GET", "/api/refunds")
    assert status == 200
    data = json.loads(body)
    assert data["total"] == 2 and data["stats"] == {"count": 2}


def test_refund_sap_trace_route(monkeypatch):
    monkeypatch.setattr(dashboard, "query", lambda sql, params=None: [])
    monkeypatch.setattr(
        dashboard.post_credit_notes_to_sap, "preview_credit_note_payload",
        lambda tid: {"payload": {"CardCode": "C0003"}, "skipped": [], "error": None},
    )
    status, body = dashboard.dispatch("GET", "/api/refunds/txn-1/sap-trace")
    assert status == 200
    data = json.loads(body)
    assert data["sap"] is None
    assert data["sap_request"]["payload"] == {"CardCode": "C0003"}


def test_stats_route_queries(monkeypatch):
    class FakeCursor:
        description = [("total_orders",)]

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return [(5,)]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    def fake_conn():
        return FakeConn()

    monkeypatch.setattr(dashboard.store, "connect", fake_conn)
    status, body = dashboard.dispatch("GET", "/api/stats")
    assert status == 200
    assert json.loads(body)["kpis"] == {"total_orders": 5}


def test_orders_filter_params_includes_has_refund():
    from urllib.parse import parse_qs

    params = dashboard._orders_filter_params(parse_qs("has_refund=yes"))
    assert params["has_refund"] == "yes"
    assert dashboard._orders_filter_params({})["has_refund"] is None


def test_sap_failures_route(monkeypatch):
    calls = {"n": 0}

    class FakeCursor:
        def execute(self, sql, params=None):
            calls["n"] += 1

        @property
        def description(self):
            return [("total",)] if calls["n"] == 1 else [("kind",), ("ref_id",), ("error",), ("posted_at",)]

        def fetchall(self):
            if calls["n"] == 1:
                return [(2,)]
            return [("invoice", "203-1234567-1234567", "insufficient stock", "2026-09-27")]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(dashboard.store, "connect", lambda: FakeConn())
    status, body = dashboard.dispatch("GET", "/api/sap-failures")
    assert status == 200
    data = json.loads(body)
    assert data["total"] == 2
    assert data["items"][0]["kind"] == "invoice"
    assert data["items"][0]["ref_id"] == "203-1234567-1234567"


def _http_event(method, path, qs="", body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "rawQueryString": qs,
        **({"body": body} if body is not None else {}),
    }


def test_dashboard_handler_routes_api(monkeypatch):
    monkeypatch.setattr(
        dashboard, "dispatch",
        lambda method, path, qs: (200, '{"ok": true}'),
    )
    resp = handlers.dashboard_handler(_http_event("GET", "/api/stats", "page=2"), None)
    assert resp["statusCode"] == 200
    assert resp["headers"]["Content-Type"] == "application/json"
    assert resp["body"] == '{"ok": true}'


def test_dashboard_handler_returns_500_json_on_crash(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "hunter2-secret")

    def boom(*a):
        raise RuntimeError("db down hunter2-secret")

    monkeypatch.setattr(dashboard, "dispatch", boom)
    resp = handlers.dashboard_handler(_http_event("GET", "/api/stats"), None)
    assert resp["statusCode"] == 500
    assert "hunter2-secret" not in resp["body"]


def test_post_via_http_is_rejected_by_handler():
    resp = handlers.dashboard_handler(_http_event("POST", "/api/post-to-sap"), None)
    assert resp["statusCode"] == 405


# ── alerts ─────────────────────────────────────────────────────────────────

def test_alert_never_raises_when_ses_fails(monkeypatch):
    import sys, types

    class _SES:
        def send_email(self, **kw):
            raise RuntimeError("ses down")

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _SES()))
    assert alerts.send_alert("x", "y") is False


def test_scrub_redacts_secrets_and_dsn_creds(monkeypatch):
    monkeypatch.setenv("LWA_REFRESH_TOKEN", "Atzr|supersecrettoken")
    out = alerts.scrub("bad token Atzr|supersecrettoken at postgresql://user:pw@host/db")
    assert "supersecrettoken" not in out and "user:pw" not in out


def test_build_report_lists_counts_and_failures():
    subject, body = alerts.build_report("post_invoices[post]", {
        "total": 3, "posted": 1, "failed": 1,
        "results": [
            {"order": "A", "status": "posted"},
            {"order": "B", "status": "failed", "error": "no batch"},
            {"order": "C", "status": "recovered"},
        ],
    })
    assert subject == "post_invoices[post] completed with FAILURES (1 failed)"
    assert "posted: 1" in body and "B: no batch" in body
    assert "failed=1" in body and "recovered=1" in body


def test_build_report_fetch_result():
    subject, body = alerts.build_report("fetch[incremental]", {"orders": 12, "items_stored": 20})
    assert subject == "fetch[incremental] completed"
    assert "orders: 12" in body


def test_categorize_failure():
    assert alerts.categorize_failure("Posting period is locked") == "SAP posting period locked"
    assert alerts.categorize_failure("The period is locked for this date") == "SAP posting period locked"
    assert alerts.categorize_failure("No SAP mapping for SKU(s): X") == "SAP item mapping"
    assert alerts.categorize_failure("Insufficient stock for A in W") is None
    assert alerts.categorize_failure("Order not found") is None


def test_failure_alert_only_mapping_and_period_lock(monkeypatch):
    sent = []
    monkeypatch.delenv("AMZ_UK_FAILURE_ALERT_EMAILS", raising=False)
    monkeypatch.setattr(alerts, "send_alert", lambda s, b, to="": sent.append((s, b, to)) or True)
    monkeypatch.setattr(alerts, "scrub", lambda t: t)
    result = {"results": [
        {"order": "1", "status": "failed", "error": "No SAP mapping for SKU(s): X"},
        {"order": "2", "status": "failed", "error": "Insufficient stock for A"},
        {"order": "3", "status": "posted"},
    ]}
    assert alerts.send_failure_alert("post_invoices[post]", result) is True
    assert sent[0][2] == "yaknaprabha@khanalfoods.com,finance@khanalfoods.com"
    assert "1:" in sent[0][1] and "2:" not in sent[0][1]
    sent.clear()
    assert alerts.send_failure_alert("post_invoices[post]", {"results": [{"order": "2", "status": "failed", "error": "x"}]}) is False
    assert alerts.send_failure_alert("fetch[incremental]", {"orders": 1}) is False
    assert not sent


def test_daily_report_one_mail_to_varshap(monkeypatch):
    from amazon_uk import daily_report
    sent = []
    monkeypatch.delenv("AMZ_UK_REPORT_EMAILS", raising=False)
    monkeypatch.setattr(daily_report, "collect_counts", lambda hours=24: {
        "orders_fetched": 12, "refunds_fetched": 2, "invoices_posted": 9, "invoices_failed": 1,
        "credit_notes_posted": 1, "credit_notes_failed": 0,
        "invoices_failed_total": 3, "credit_notes_failed_total": 0,
        "invoices_dead": 2, "credit_notes_dead": 0})
    monkeypatch.setattr(alerts, "send_alert", lambda s, b, to="": sent.append((s, b, to)) or True)
    monkeypatch.setattr(alerts, "scrub", lambda t: t)
    out = daily_report.run()
    assert out["emailed"] is True and len(sent) == 1
    assert sent[0][2] == "varshap@khanalfoods.com"
    assert "12 fetched, 10 posted, 1 failed" in sent[0][0]


def test_default_sender(monkeypatch):
    monkeypatch.delenv("AMZ_UK_ALERT_FROM_EMAIL", raising=False)
    monkeypatch.setenv("AMZ_UK_ALERT_ADMIN_EMAIL", "a@x.com")
    import sys, types
    calls = {}
    class _SES:
        def send_email(self, **kw): calls.update(kw)
    fake = types.SimpleNamespace(client=lambda *a, **k: _SES())
    monkeypatch.setitem(sys.modules, "boto3", fake)
    assert alerts.send_alert("s", "b") is True
    assert calls["Source"] == "varshap@khanalfoods.com"


def test_daily_report_mentions_dead(monkeypatch):
    from amazon_uk import daily_report
    counts = {"orders_fetched": 1, "refunds_fetched": 0, "invoices_posted": 0, "invoices_failed": 0,
              "credit_notes_posted": 0, "credit_notes_failed": 0, "invoices_failed_total": 2,
              "credit_notes_failed_total": 0, "invoices_dead": 2, "credit_notes_dead": 1}
    _, body = daily_report.build(counts)
    assert "invoices=2, credit notes=1" in body and "Dead" in body


def test_dead_alert_only_for_dead_rows(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_alert", lambda s, b, to="": sent.append((s, b, to)) or True)
    monkeypatch.setattr(alerts, "scrub", lambda t: t)
    result = {"results": [
        {"order": "A", "status": "failed", "error": "boom", "attempts": 5, "dead": True},
        {"order": "B", "status": "failed", "error": "boom", "attempts": 2, "dead": False},
        {"order": "C", "status": "posted"},
    ]}
    assert alerts.send_dead_alert("post_invoices[post]", result) is True
    assert "A (5 attempts)" in sent[0][1] and "B" not in sent[0][1]
    sent.clear()
    assert alerts.send_dead_alert("post_invoices[post]", {"results": [{"order": "B", "status": "failed", "dead": False}]}) is False
    assert alerts.send_dead_alert("fetch[incremental]", {"orders": 3}) is False
    assert not sent

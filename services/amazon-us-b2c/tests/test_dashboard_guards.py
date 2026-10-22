"""Dashboard API guards: the query string must not be able to reach the ops-only
fetch/recon modes, ask for an unbounded batch, or presign arbitrary S3 keys.

Before: sync-orders forwarded any ?mode= straight into amazonFetch (including
seed_watermark, which moves the watermark to "now" and silently skips every
unsynced order, and backfill, which burns SP-API quota), run-recon forwarded
any mode, post-ready / retry-invoices took an unbounded limit, and
recon-download only accepted keys under recon/ although exports are written
under recon-export/ and inventory-export/."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import handlers  # noqa: E402


def _event(path, method="POST", **query):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": {k: str(v) for k, v in query.items()},
    }


@pytest.fixture
def invoked(monkeypatch):
    calls = []
    monkeypatch.setattr(handlers, "_invoke_async", lambda suffix, payload: calls.append((suffix, payload)))
    return calls


def _body(resp):
    return json.loads(resp["body"])


@pytest.mark.parametrize("mode", ["seed_watermark", "backfill", "resume_reports", "", "nonsense"])
def test_sync_orders_refuses_ops_only_modes(invoked, mode):
    resp = handlers.dashboard_handler(_event("/v1/amazon/sync-orders", mode=mode), None)
    if mode == "":
        # no mode given means the default incremental sync
        assert resp["statusCode"] == 202 and invoked[0][1]["mode"] == "incremental"
        return
    assert resp["statusCode"] == 400
    assert invoked == []


@pytest.mark.parametrize("mode", ["incremental", "report"])
def test_sync_orders_still_allows_what_the_dashboard_uses(invoked, mode):
    resp = handlers.dashboard_handler(_event("/v1/amazon/sync-orders", mode=mode, from_date="2026-10-01", to_date="2026-10-02"), None)
    assert resp["statusCode"] == 202
    assert invoked == [("amazonFetch", {"mode": mode, "from_date": "2026-10-01", "to_date": "2026-10-02"})]


def test_run_recon_refuses_unknown_modes(invoked):
    resp = handlers.dashboard_handler(_event("/v1/amazon/run-recon", mode="drop_everything"), None)
    assert resp["statusCode"] == 400
    assert invoked == []


@pytest.mark.parametrize("mode", ["invariants", "mtr"])
def test_run_recon_still_allows_what_the_dashboard_uses(invoked, mode):
    resp = handlers.dashboard_handler(_event("/v1/amazon/run-recon", mode=mode), None)
    assert resp["statusCode"] == 202
    assert invoked[0][1]["mode"] == mode


def test_post_ready_caps_the_batch_size(invoked):
    handlers.dashboard_handler(_event("/v1/amazon/post-ready", limit=100000), None)
    assert invoked[0][1]["limit"] == 200


def test_post_ready_ignores_a_garbage_limit(invoked):
    resp = handlers.dashboard_handler(_event("/v1/amazon/post-ready", limit="abc"), None)
    assert resp["statusCode"] == 202
    assert 1 <= invoked[0][1]["limit"] <= 200


def test_retry_invoices_caps_the_batch_size(monkeypatch):
    seen = {}

    class _Ctx:
        def __enter__(self):
            return "conn"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(handlers.store, "connect", lambda: _Ctx())
    monkeypatch.setattr(
        handlers.post_invoices, "retry_failed_orders", lambda conn, limit: seen.update(limit=limit) or {"ok": True}
    )

    resp = handlers.dashboard_handler(_event("/v1/amazon/retry-invoices", limit=100000), None)

    assert resp["statusCode"] == 200
    assert seen["limit"] == 200


@pytest.mark.parametrize("key", ["recon/2026-09.csv", "recon-export/2026-09.xlsx", "inventory-export/2026-09.xlsx"])
def test_recon_download_allows_the_prefixes_this_service_writes(monkeypatch, key):
    import boto3

    class _S3:
        def generate_presigned_url(self, op, Params, ExpiresIn):
            return "https://signed/" + Params["Key"]

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _S3())
    resp = handlers.dashboard_handler(_event("/v1/amazon/recon-download", method="GET", key=key), None)
    assert resp["statusCode"] == 200
    assert _body(resp)["url"].endswith(key)


@pytest.mark.parametrize("key", ["", "secrets/x", "recon/../secrets/x", "recon-export/../../x", "/recon/x"])
def test_recon_download_refuses_other_keys_and_traversal(monkeypatch, key):
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not presign")))
    resp = handlers.dashboard_handler(_event("/v1/amazon/recon-download", method="GET", key=key), None)
    assert resp["statusCode"] == 400

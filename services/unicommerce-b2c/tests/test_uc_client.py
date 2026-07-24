"""Tests for UcClient's date-range chunking and error surfacing.

Both bugs were found 2026-07-24 testing a fresh Lambda's cold-start fetch
against the real Unicommerce API: get_orders_between sent a single
60-day request (fetch_sync's default lookback), which UC rejects
(SALE_ORDER_SEARCH_DATE_RANGE_LIMIT_EXCEEDED, max 31 days) -- and because
that error comes back as HTTP 200 with "successful": false, the old code
read it as zero results instead of an error. Same 60-day default exists
in the Frappe source (workers/fetch.py), so it's latent there too."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.credentials import UcCredentials  # noqa: E402
from unicommerce.uc_client import UcApiError, UcClient  # noqa: E402


def _client() -> UcClient:
    c = UcClient(UcCredentials(base_url="https://uc.example.com", username="u", password="p", facility_code=""))
    c.token = "fake-token"  # skip real login
    return c


def test_get_orders_between_chunks_a_65_day_span():
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_window(from_date, to_date, channel=None):
        calls.append((from_date, to_date))
        return []

    client._search_orders_window = fake_window
    client.get_orders_between("2026-05-20 00:00:00", "2026-07-24 00:00:00")

    assert len(calls) == 3  # 65 days over a 30-day chunk size -> 3 windows
    # No gaps: each window starts 1 second after the previous one ended.
    assert calls[0][0] == "2026-05-20 00:00:00"
    assert calls[-1][1] == "2026-07-24 00:00:00"
    for (_, prev_end), (next_start, _) in zip(calls, calls[1:]):
        assert next_start > prev_end


def test_get_orders_between_60_day_span_is_exactly_two_windows():
    client = _client()
    calls: list[tuple[str, str]] = []
    client._search_orders_window = lambda from_date, to_date, channel=None: calls.append((from_date, to_date)) or []

    client.get_orders_between("2026-05-25 00:00:00", "2026-07-24 00:00:00")

    assert len(calls) == 2


def test_get_orders_between_single_call_within_limit():
    client = _client()
    calls = []
    client._search_orders_window = lambda from_date, to_date, channel=None: calls.append((from_date, to_date)) or []

    client.get_orders_between("2026-07-01 00:00:00", "2026-07-10 00:00:00")

    assert len(calls) == 1


def test_get_orders_between_aggregates_results_across_chunks():
    client = _client()
    windows = iter([[{"code": "A"}], [{"code": "B"}]])
    client._search_orders_window = lambda from_date, to_date, channel=None: next(windows)

    result = client.get_orders_between("2026-05-25 00:00:00", "2026-07-24 00:00:00")

    assert result == [{"code": "A"}, {"code": "B"}]


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json = json_body
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


def test_post_with_reauth_raises_on_successful_false(monkeypatch):
    client = _client()
    body = {
        "successful": False,
        "errors": [{"message": "SALE_ORDER_SEARCH_DATE_RANGE_LIMIT_EXCEEDED", "description": "Date range exceeds the permitted maximum range of 31 days"}],
        "elements": [],
    }
    monkeypatch.setattr("unicommerce.uc_client.requests.post", lambda *a, **k: _FakeResponse(body))

    try:
        client._post_with_reauth("services/rest/v1/oms/saleOrder/search", body={})
        assert False, "expected UcApiError"
    except UcApiError as e:
        assert "31 days" in str(e)


def test_post_with_reauth_passes_through_successful_true(monkeypatch):
    client = _client()
    body = {"successful": True, "elements": [{"code": "X"}]}
    monkeypatch.setattr("unicommerce.uc_client.requests.post", lambda *a, **k: _FakeResponse(body))

    result = client._post_with_reauth("services/rest/v1/oms/saleOrder/search", body={})

    assert result == body

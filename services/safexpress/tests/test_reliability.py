import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from safexpress import client, pod_service, store  # noqa: E402
from safexpress.config import MAX_LOOKBACK_DAYS  # noqa: E402
from safexpress.exceptions import SafexpressAPIError, SafexpressWaybillNotFoundError  # noqa: E402

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(client.time, "sleep", lambda _s: None)


def post_returning(monkeypatch, *responses):
    """requests.post answers with each response in turn (the last one repeats)."""
    queue = list(responses)
    calls = []

    def fake(url, **kwargs):
        calls.append(kwargs["json"]["docNo"])
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(client.requests, "post", fake)
    return calls


def test_a_real_answer_without_a_waybill_is_not_found(monkeypatch):
    post_returning(monkeypatch, FakeResponse(200, {"shipment": {}}))
    with pytest.raises(SafexpressWaybillNotFoundError):
        client.get_tracking_by_invoice("KAIN26", "1")


def test_a_waybill_is_returned_with_the_series_that_matched(monkeypatch):
    post_returning(monkeypatch, FakeResponse(200, {"shipment": {}}), FakeResponse(200, {"shipment": {"waybill": "W1"}}))
    result = client.get_tracking_by_invoice("KAIN26", "1")
    assert result["shipment"]["waybill"] == "W1"
    assert result["_successful_series"] == "kain26"  # the first variant answered "none", the second matched


@pytest.mark.parametrize("failure", [FakeResponse(500, text="boom"), FakeResponse(503), FakeResponse(200, None), client.requests.exceptions.ConnectionError("down")])
def test_an_outage_is_an_error_not_no_waybill(monkeypatch, failure):
    calls = post_returning(monkeypatch, failure)
    with pytest.raises(SafexpressAPIError) as raised:
        client.get_tracking_by_invoice("KAIN26", "1")
    assert not isinstance(raised.value, SafexpressWaybillNotFoundError)
    assert len(calls) == 3  # retried, and it stopped at the first variant each time instead of trying every series


def test_a_client_error_is_not_retried(monkeypatch):
    calls = post_returning(monkeypatch, FakeResponse(400, text="bad"))
    with pytest.raises(SafexpressAPIError):
        client.get_tracking_by_invoice("KAIN26", "1")
    assert len(calls) == 1


def test_discovery_looks_back_over_the_whole_silence():
    assert pod_service.lookback_days(4, NOW - timedelta(hours=3), NOW) == 4  # healthy: the small window
    assert pod_service.lookback_days(4, NOW - timedelta(days=22), NOW) == 23  # outage: cover it all
    assert pod_service.lookback_days(4, None, NOW) == 4  # empty table
    assert pod_service.lookback_days(4, NOW - timedelta(days=9000), NOW) == MAX_LOOKBACK_DAYS


def test_failed_rows_are_open_so_they_get_retried():
    assert "failed" in store.OPEN_STATUSES

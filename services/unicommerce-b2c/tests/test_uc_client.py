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


# ── PoGrn / InventorySync write methods ─────────────────────────────────

def _capture_post(monkeypatch, response_body):
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        return _FakeResponse(response_body)

    monkeypatch.setattr("unicommerce.uc_client.requests.post", fake_post)
    return calls


def test_create_po_approved_aggregates_and_sets_facility_header(monkeypatch):
    client = _client()
    calls = _capture_post(monkeypatch, {"successful": True, "purchaseOrderCode": "PO123"})

    result = client.create_po_approved(
        "EC-B2C", [{"itemSKU": "FGHN0035", "quantity": 54.0}], facility_code="kfl_ecom_warehouse"
    )

    assert result["purchaseOrderCode"] == "PO123"
    assert len(calls) == 1
    assert calls[0]["url"].endswith("purchaseOrder/createApproved")
    assert calls[0]["headers"]["Facility"] == "kfl_ecom_warehouse"
    assert calls[0]["json"]["vendorCode"] == "EC-B2C"
    assert calls[0]["json"]["purchaseOrderItems"] == [
        {"itemSKU": "FGHN0035", "quantity": 54.0, "unitPrice": 0, "taxTypeCode": None}
    ]


def test_create_inflow_receipt_sends_wsgrn_shape(monkeypatch):
    client = _client()
    calls = _capture_post(monkeypatch, {"successful": True, "inflowReceiptCode": "GRN1"})

    result = client.create_inflow_receipt("PO123", "GRN-53974-20260727-R1", "2026-07-27T00:00:00.000Z", facility_code="AMZ-BLR7")

    assert result["inflowReceiptCode"] == "GRN1"
    assert calls[0]["url"].endswith("inflowReceipt/create")
    assert calls[0]["json"]["purchaseOrderCode"] == "PO123"
    assert calls[0]["json"]["wsGRN"]["vendorInvoiceNumber"] == "GRN-53974-20260727-R1"
    assert calls[0]["headers"]["Facility"] == "AMZ-BLR7"


def test_add_grn_item_wraps_item_under_inflow_receipt_code(monkeypatch):
    client = _client()
    calls = _capture_post(monkeypatch, {"successful": True})
    item = {"skuCode": "FGHN0035", "quantity": 54.0}

    client.add_grn_item("GRN1", item, facility_code="kfl_ecom_warehouse")

    assert calls[0]["url"].endswith("inflowReceipt/addItemSKU")
    assert calls[0]["json"] == {"inflowReceiptCode": "GRN1", "inflowReceiptItem": item}


def test_adjust_inventory_bulk_sends_adjustments_list(monkeypatch):
    client = _client()
    calls = _capture_post(monkeypatch, {"successful": True, "inventoryAdjustmentResponses": []})
    adjustments = [{"itemSKU": "FGHN0035", "quantity": 5, "adjustmentType": "REMOVE"}]

    client.adjust_inventory_bulk(adjustments, facility_code="AMZ-BLR7")

    assert calls[0]["url"].endswith("inventory/adjust/bulk")
    assert calls[0]["json"] == {"inventoryAdjustments": adjustments}
    assert calls[0]["headers"]["Facility"] == "AMZ-BLR7"


def test_po_grn_write_methods_raise_on_successful_false(monkeypatch):
    client = _client()
    _capture_post(monkeypatch, {"successful": False, "message": "vendor not found"})

    try:
        client.create_po_approved("EC-B2C", [{"itemSKU": "X", "quantity": 1}], facility_code="kfl_ecom_warehouse")
        assert False, "expected UcApiError"
    except UcApiError as e:
        assert "vendor not found" in str(e)

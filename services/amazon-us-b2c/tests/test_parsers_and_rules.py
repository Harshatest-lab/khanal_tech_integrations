"""Pure-function coverage: state codes, refund parsing, report parsing,
FBA ship dates, SKU cleaning, MTR recon comparison, date chunking."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from amazon_us.dates import date_chunks, pacific_day_to_utc, utc_to_pacific  # noqa: E402
from amazon_us.fba_ship_sync import parse_earliest_ship_dates  # noqa: E402
from amazon_us.finances_api import _parse_refund_event, clamp_posted_before  # noqa: E402
from amazon_us.hana_access import allocate_from_rows, clean_sku  # noqa: E402
from amazon_us.recon import compare, parse_mtr  # noqa: E402
from amazon_us.report_parser import parse_orders_report  # noqa: E402
from amazon_us.state_codes import normalize_us_state_code  # noqa: E402


# ── State codes ──────────────────────────────────────────────────────────

def test_state_normalization():
    assert normalize_us_state_code("California") == "CA"
    assert normalize_us_state_code("california") == "CA"
    assert normalize_us_state_code("ca") == "CA"
    assert normalize_us_state_code("N.Y.") == "NY"
    assert normalize_us_state_code("washington dc") == "DC"
    assert normalize_us_state_code("Ontario") == "Ontario"  # unknown passes through
    assert normalize_us_state_code(None) is None


# ── Finances refund parsing ──────────────────────────────────────────────

def test_refund_event_parses_shipping_charge_types():
    raw = {
        "AmazonOrderId": "111-1-1",
        "PostedDate": "2026-07-01T10:00:00Z",
        "ShipmentItemAdjustmentList": [
            {
                "SellerSKU": "SKU-A",
                "ItemChargeAdjustmentList": [
                    {"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": -10.0}},
                    {"ChargeType": "Tax", "ChargeAmount": {"CurrencyAmount": -0.8}},
                    {"ChargeType": "ShippingCharge", "ChargeAmount": {"CurrencyAmount": -3.99}},
                    {"ChargeType": "ShippingTax", "ChargeAmount": {"CurrencyAmount": -0.25}},
                ],
            }
        ],
    }
    event = _parse_refund_event(raw)
    adj = event.item_adjustments[0]
    assert adj.principal_amount == -10.0
    assert adj.tax_amount == -0.8
    assert adj.shipping_amount == -3.99  # was silently dropped pre-fix
    assert adj.shipping_tax_amount == -0.25
    assert event.refund_id == "111-1-1-2026-07-01T10:00:00Z"


def test_shipping_only_refund_still_parses():
    raw = {
        "AmazonOrderId": "111-1-1",
        "PostedDate": "2026-07-01T10:00:00Z",
        "ShipmentItemAdjustmentList": [
            {
                "SellerSKU": "SKU-A",
                "ItemChargeAdjustmentList": [
                    {"ChargeType": "ShippingCharge", "ChargeAmount": {"CurrencyAmount": -2.99}},
                ],
            }
        ],
    }
    assert _parse_refund_event(raw) is not None


def test_posted_before_clamped_to_five_minutes_ago():
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    clamped = datetime.fromisoformat(clamp_posted_before(future).replace("Z", "+00:00"))
    assert clamped < datetime.now(timezone.utc) - timedelta(minutes=4)
    past = "2026-06-01T00:00:00Z"
    assert clamp_posted_before(past) == past


class _FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return {"payload": self._payload}


def _refund_page(order_id, next_token=None):
    return {
        "FinancialEvents": {
            "RefundEventList": [
                {
                    "AmazonOrderId": order_id,
                    "PostedDate": "2026-07-01T10:00:00Z",
                    "ShipmentItemAdjustmentList": [
                        {
                            "SellerSKU": "SKU-A",
                            "ItemChargeAdjustmentList": [
                                {"ChargeType": "Principal", "ChargeAmount": {"CurrencyAmount": -10.0}},
                            ],
                        }
                    ],
                }
            ]
        },
        "NextToken": next_token,
    }


def test_list_refund_events_retries_transient_page_failure_then_succeeds(monkeypatch):
    # Real 2026-08-14 bug: a mid-pagination page 400'd once with an empty
    # reason phrase and killed the whole fetch, discarding every refund
    # already collected. A single retry should recover transparently.
    import amazon_us.finances_api as finances_api

    calls = {"n": 0}

    def fake_get(path, params):
        calls["n"] += 1
        if "NextToken" in params:
            if calls["n"] == 2:
                return _FakeResponse(400, text="")
            return _FakeResponse(200, _refund_page("222-2-2"))
        return _FakeResponse(200, _refund_page("111-1-1", next_token="tok1"))

    monkeypatch.setattr(finances_api.sp_api, "get", fake_get)
    monkeypatch.setattr(finances_api.time, "sleep", lambda _: None)

    events = finances_api.list_refund_events("2026-07-01T00:00:00Z")
    assert [e.amazon_order_id for e in events] == ["111-1-1", "222-2-2"]


def test_list_refund_events_returns_partial_results_when_page_permanently_fails(monkeypatch):
    # If a page keeps 400ing across every retry, don't raise and lose the
    # events already collected from earlier pages -- post_refunds is
    # idempotent by refund_id, so returning the partial list lets the
    # caller post what it found instead of the whole run dying.
    import amazon_us.finances_api as finances_api

    def fake_get(path, params):
        if "NextToken" in params:
            return _FakeResponse(400, text='{"errors": [{"message": "stale token"}]}')
        return _FakeResponse(200, _refund_page("111-1-1", next_token="tok1"))

    monkeypatch.setattr(finances_api.sp_api, "get", fake_get)
    monkeypatch.setattr(finances_api.time, "sleep", lambda _: None)

    events = finances_api.list_refund_events("2026-07-01T00:00:00Z")
    assert [e.amazon_order_id for e in events] == ["111-1-1"]


# ── Orders flat-file report ──────────────────────────────────────────────

_REPORT = (
    "amazon-order-id\torder-status\tpurchase-date\tsku\tasin\tproduct-name\tquantity\t"
    "item-price\titem-tax\tshipping-price\tshipping-tax\tship-city\tship-state\t"
    "ship-postal-code\tship-country\torder-item-id\n"
    "111-1-1\tShipped\t2026-06-10T15:00:00Z\tSKU-A\tB0TEST\tDental Chew\t2\t"
    "26.99\t1.18\t3.99\t0.25\tSacramento\tCA\t94203\tUS\tOI-1\n"
    "111-1-1\tShipped\t2026-06-10T15:00:00Z\tSKU-B\tB0TES2\tPuffies\t1\t"
    "9.99\t0.00\t0.00\t0.00\tSacramento\tCA\t94203\tUS\tOI-2\n"
)


def test_report_parser_groups_lines_and_gross_unit_price():
    orders = parse_orders_report(_REPORT)
    order = orders["111-1-1"]
    assert order["order_status"] == "Shipped"
    assert order["ship_state"] == "CA"
    assert len(order["lines"]) == 2
    line = order["lines"][0]
    assert line["unit_price"] == round(26.99 / 2, 5)  # gross
    assert line["shipping_price_amount"] == 3.99


# ── FBA ship dates ───────────────────────────────────────────────────────

def test_fba_parse_keeps_earliest_pacific_date():
    tsv = (
        "amazon-order-id\tshipment-date\n"
        "111-1-1\t2026-06-13T02:00:00Z\n"   # Jun 12 Pacific
        "111-1-1\t2026-06-14T22:00:00Z\n"
        "222-2-2\t2026-06-20T10:00:00Z\n"
    )
    dates = parse_earliest_ship_dates(tsv)
    assert dates["111-1-1"] == "2026-06-12"
    assert dates["222-2-2"] == "2026-06-20"


# ── SKU cleaning + FEFO ──────────────────────────────────────────────────

def test_clean_sku_strips_control_chars():
    assert clean_sku("\rFBA_DC_M_1P") == "FBA_DC_M_1P"  # the 94-row Excel artifact
    assert clean_sku("  SKU-A ") == "SKU-A"
    assert clean_sku(None) == ""


def test_fefo_allocation():
    rows = [("B1", 1.0), ("B2", 5.0)]
    alloc = allocate_from_rows(rows, 3, "FG1", "AMZ-FBA")
    assert [(a.batch_number, a.quantity) for a in alloc] == [("B1", 1.0), ("B2", 2.0)]
    assert allocate_from_rows([], 3, "FG1", "AMZ-FBA") == []  # not batch-managed


def test_fefo_insufficient_stock_raises():
    import pytest

    from amazon_us.hana_access import InsufficientStockError

    with pytest.raises(InsufficientStockError):
        allocate_from_rows([("B1", 1.0)], 3, "FG1", "AMZ-FBA")


# ── Dates ────────────────────────────────────────────────────────────────

def test_date_chunks_max_30_days():
    chunks = date_chunks("2026-05-01", "2026-06-30", max_days=30)
    assert chunks[0] == ("2026-05-01", "2026-05-30")
    assert chunks[-1][1] == "2026-06-30"
    assert all(
        (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days < 30
        for a, b in chunks
    )


def test_pacific_conversions_roundtrip():
    assert utc_to_pacific("2026-06-13T02:00:00Z").strftime("%Y-%m-%d") == "2026-06-12"
    assert pacific_day_to_utc("2026-06-12").endswith("Z")


# ── MTR recon ────────────────────────────────────────────────────────────

_MTR = (
    'some preamble line\n'
    '"date/time","type","order id","product sales","product sales tax",'
    '"shipping credits","shipping credits tax","gift wrap credits","giftwrap credits tax"\n'
    '"Jun 5, 2026","Order","111-1-1","26.99","1.18","3.99","0.25","0","0"\n'
    '"Jun 20, 2026","Refund","111-1-1","-13.49","-0.59","0","0","0","0"\n'
    '"Jun 6, 2026","Order","222-2-2","9.99","0","0","0","0","0"\n'
)


def test_parse_mtr_aggregates_gross_per_order():
    orders = parse_mtr(_MTR)
    assert orders["111-1-1"]["sales"] == round(26.99 + 1.18 + 3.99 + 0.25, 2)
    assert orders["111-1-1"]["refunds"] == round(13.49 + 0.59, 2)
    assert orders["222-2-2"]["sales"] == 9.99


def test_compare_categories():
    mtr = {
        "111-1-1": {"sales": 32.41, "refunds": 14.08},
        "222-2-2": {"sales": 9.99, "refunds": 0.0},
        "333-3-3": {"sales": 0.0, "refunds": 5.00},   # refund-only, pre-cutoff order
        "444-4-4": {"sales": 20.0, "refunds": 0.0},   # not in SAP at all
    }
    sap = {
        "111-1-1": {"sales": 32.41, "refunds": 14.08},
        "222-2-2": {"sales": 9.99, "refunds": 9.99},  # refunded after MTR pulled
        "555-5-5": {"sales": 15.0, "refunds": 0.0},   # adjacent-month timing
    }
    dates = {"333-3-3": "2026-04-10"}
    by_id = {row["order_id"]: row["category"] for row in compare(mtr, sap, "2026-06-01", dates)}
    assert by_id["111-1-1"] == "matched"
    assert by_id["222-2-2"] == "refunded_after_mtr_extract"
    assert by_id["333-3-3"] == "pre_cutoff_excluded"
    assert by_id["444-4-4"] == "missing_in_sap"
    assert by_id["555-5-5"] == "sap_not_in_mtr_timing"

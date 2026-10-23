"""Tests for return-payload extraction (fetch_returns' pure helpers),
ported from khanal_tech_integrations/unicommerce/workers/fetch.py."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from unicommerce.fetch_sync import incremental_returns_since  # noqa: E402
from unicommerce.returns import (  # noqa: E402
    extract_return_date,
    extract_return_ids,
    extract_return_items,
    extract_return_type,
)


def test_extract_return_ids_direct_from_rsov():
    ret = {"returnSaleOrderValue": {"reversePickupCode": "RP-1", "saleOrderCode": "SO-9"}}
    rid, oid, codes = extract_return_ids(ret)
    assert rid == "RP-1"
    assert oid == "SO-9"
    assert codes == []


def test_extract_return_ids_falls_back_to_item_codes_when_no_order_code():
    ret = {
        "returnSaleOrderValue": {"shipmentCode": "SH-2"},
        "returnSaleOrderItems": [{"saleOrderItemCode": "IC-2"}, {"code": "IC-1"}, {"saleOrderItemCode": "IC-2"}],
    }
    rid, oid, codes = extract_return_ids(ret)
    assert rid == "SH-2"
    assert oid is None
    assert codes == ["IC-1", "IC-2"]  # deduped + sorted for the caller's DB lookup


def test_extract_return_date_prefers_completed_over_created():
    ret = {"returnSaleOrderValue": {"returnCompletedDate": "2026-06-15T08:00:00Z", "returnCreatedDate": "2026-06-01T00:00:00Z"}}
    assert extract_return_date(ret) == "2026-06-15"


def test_extract_return_date_handles_epoch_millis():
    # 2026-06-15T00:00:00Z in ms
    ms = 1781481600000
    ret = {"returnSaleOrderValue": {"returnCompletedDate": ms}}
    assert extract_return_date(ret) == "2026-06-15"


def test_extract_return_date_empty_defaults_to_today():
    assert extract_return_date({}) == date.today().isoformat()


def test_extract_return_type_rto_vs_customer():
    assert extract_return_type({"returnSaleOrderValue": {"rtoReason": "undelivered"}}) == "courier"
    assert extract_return_type({"returnSaleOrderValue": {}}) == "customer"


def test_extract_return_items_reads_return_sale_order_items():
    ret = {"returnSaleOrderItems": [{"skuCode": "A"}]}
    assert extract_return_items(ret) == [{"skuCode": "A"}]
    assert extract_return_items({}) == []


# ── Incremental window start ─────────────────────────────────────────────
# The returns cursor sat unused from 2026-08-19 to 2026-10-23 (every run in between was a manual window), so a
# schedule starting on it would ask UC for two months of "updated" returns and re-stage old ones.

_NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)


def test_incremental_returns_window_uses_a_fresh_cursor_as_is():
    assert incremental_returns_since("2026-10-23 11:15:00", _NOW) == "2026-10-23 11:15:00"


def test_incremental_returns_window_clamps_a_stale_cursor_to_the_lookback_limit(caplog):
    with caplog.at_level("WARNING"):
        since = incremental_returns_since("2026-08-19 05:43:00", _NOW)
    assert since == "2026-10-16 12:00:00"  # 7 days before _NOW, not two months
    assert any("stale" in r.getMessage().lower() for r in caplog.records)


def test_incremental_returns_window_without_a_cursor_starts_at_the_lookback_limit():
    assert incremental_returns_since(None, _NOW) == "2026-10-16 12:00:00"
    assert incremental_returns_since("", _NOW) == "2026-10-16 12:00:00"

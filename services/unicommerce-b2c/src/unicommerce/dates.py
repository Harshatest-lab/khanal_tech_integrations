"""Datetime helpers ported from khanal_tech_integrations/unicommerce/workers/fetch.py.

UC timestamps are epoch-millis in some payloads, ISO strings in others.
Order dates specifically are IST business dates (see _epoch_millis_to_date's
docstring in the Frappe source) -- converting via naive fromtimestamp()
would depend on the server's local timezone (UTC on AWS Lambda), rolling
late-night IST orders over to the next day. Pinning to IST fixes this,
same as the Frappe worker does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def parse_source_datetime(raw_value) -> datetime | None:
    """Epoch-millis (int/float/digit-string) or ISO string -> aware UTC datetime."""
    if not raw_value:
        return None
    if isinstance(raw_value, datetime):
        return raw_value
    if isinstance(raw_value, (int, float)) or (isinstance(raw_value, str) and str(raw_value).isdigit()):
        try:
            epoch = int(raw_value)
            if epoch > 10_000_000_000:
                epoch = int(epoch / 1000)
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except Exception:
        return None
    # UC sometimes sends ISO strings with no offset at all (unlike the epoch
    # path above, which always produces an aware datetime) -- fromisoformat
    # then returns naive, and comparing that against the aware datetimes
    # elsewhere in the cursor logic crashes with "can't compare offset-naive
    # and offset-aware datetimes" (hit live in fetch_returns). This function
    # promises an aware UTC datetime; enforce it on every path.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def format_cursor(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def epoch_millis_to_ist_date(value) -> str | None:
    """UC epoch-millis -> 'YYYY-MM-DD' date string, pinned to IST."""
    try:
        millis = int(float(value or 0))
    except Exception:
        return None
    if millis <= 0:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000.0, tz=IST).date().isoformat()
    except Exception:
        return None


def extract_order_date(order: dict) -> str:
    """Priority: displayOrderDateTime (channel order date) -> created (UC
    intake timestamp) -> orderDate. These can differ by days; using the
    same priority the SAP payload builder will eventually use keeps
    Frappe order_date and the future SAP DocDate in agreement."""
    raw = order.get("displayOrderDateTime") or order.get("created") or order.get("orderDate") or ""
    if not raw:
        return datetime.now(IST).date().isoformat()
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and str(raw).isdigit()):
        result = epoch_millis_to_ist_date(raw)
        return result or datetime.now(IST).date().isoformat()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return str(raw)[:10]


def extract_return_date(ret: dict) -> str:
    """Prefer the most final event: completedDate > receivedDate >
    deliveryDate > createdDate -- matches the SAP CN DocDate the Frappe
    worker uses (date goods were confirmed back in warehouse)."""
    rsov = ret.get("returnSaleOrderValue") or {}
    raw = (
        rsov.get("returnCompletedDate")
        or rsov.get("inventoryReceivedDate")
        or rsov.get("returnDeliveryDate")
        or rsov.get("returnCreatedDate")
        or ret.get("created")
        or ""
    )
    if not raw:
        return datetime.now(IST).date().isoformat()
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and str(raw).isdigit()):
        result = epoch_millis_to_ist_date(raw)
        return result or datetime.now(IST).date().isoformat()
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return str(raw)[:10]

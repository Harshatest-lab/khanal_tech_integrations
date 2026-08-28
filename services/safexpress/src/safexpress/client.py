"""SafeExpress API client: tracking-by-invoice, tracking-by-waybill, POD download.

Ported from safexpress2/api_client.py — same request shapes, same retry
behavior, same series-name-variant fallback (an invoice's series prefix can
come back upper/lower/as-given depending on how SafeExpress registered it).
"""

from __future__ import annotations

import base64
import logging
import time
from functools import wraps
from typing import Any, Callable, TypeVar

import requests

from .config import (
    DEFAULT_POD_FETCH_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DOC_TYPE_INVOICE,
    DOC_TYPE_WAYBILL,
    INVOICE_SERIES_PREFIXES,
    MAX_RETRY_ATTEMPTS,
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_DELAY_SECONDS,
    TRACKING_API_URL,
)
from .exceptions import SafexpressAPIError, SafexpressPODNotFoundError, SafexpressWaybillNotFoundError

logger = logging.getLogger(__name__)
T = TypeVar("T")

_BASE_HEADERS = {"Accept": "*/*", "User-Agent": "Khanal Tech", "Content-Type": "application/json"}


def _retry(max_attempts: int = MAX_RETRY_ATTEMPTS, delay: float = RETRY_DELAY_SECONDS) -> Callable:
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            current_delay = delay
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    is_retryable = getattr(e, "is_retryable", lambda: True)()
                    if not is_retryable:
                        raise
                    last_exc = e
                    if attempt < max_attempts - 1:
                        logger.warning(
                            "Attempt %d/%d failed for %s: %s. Retrying in %.1fs",
                            attempt + 1, max_attempts, fn.__name__, e, current_delay,
                        )
                        time.sleep(current_delay)
                        current_delay *= RETRY_BACKOFF_MULTIPLIER
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator


def _series_variants(series_name: str) -> list[str]:
    if not series_name or not series_name.strip():
        return list(INVOICE_SERIES_PREFIXES)
    variants = [series_name]
    for v in (series_name.upper(), series_name.lower()):
        if v not in variants:
            variants.append(v)
    return variants


def _format_invoice_number(series_name: str, doc_number: str) -> str:
    if not series_name or not series_name.strip():
        return str(doc_number)
    return f"{series_name}/{doc_number}"


@_retry()
def get_tracking_by_invoice(series_name: str, doc_number: str) -> dict[str, Any]:
    """Tries series-name variants (as-given, upper, lower), or every
    configured prefix when series_name is unknown. Raises
    SafexpressWaybillNotFoundError (not retryable — this is normal for an
    invoice that hasn't shipped via SafeExpress yet) if none match."""
    tried: list[str] = []
    for variant in _series_variants(series_name):
        formatted = _format_invoice_number(variant, doc_number)
        tried.append(formatted)
        try:
            resp = requests.post(
                TRACKING_API_URL, headers=_BASE_HEADERS,
                json={"docNo": formatted, "docType": DOC_TYPE_INVOICE},
                timeout=DEFAULT_TIMEOUT_SECONDS, verify=False,
            )
        except requests.exceptions.RequestException as e:
            logger.warning("Tracking request failed for %s: %s", formatted, e)
            continue

        if resp.status_code != 200:
            logger.warning("Tracking API status %d for %s: %s", resp.status_code, formatted, resp.text[:300])
            continue

        result = resp.json()
        shipment = result.get("shipment") or {}
        if shipment.get("waybill"):
            result["_successful_series"] = variant
            return result

    raise SafexpressWaybillNotFoundError(
        f"No waybill found for invoice {doc_number} (tried: {', '.join(tried)})",
        invoice_number=doc_number,
    )


@_retry()
def get_tracking_by_waybill(waybill_number: str) -> dict[str, Any]:
    try:
        resp = requests.post(
            TRACKING_API_URL, headers=_BASE_HEADERS,
            json={"docNo": waybill_number, "docType": DOC_TYPE_WAYBILL},
            timeout=DEFAULT_TIMEOUT_SECONDS, verify=False,
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise SafexpressAPIError(f"Failed to get tracking for waybill {waybill_number}: {e}") from e

    result = resp.json()
    shipment = result.get("shipment") or {}
    if shipment.get("message") == "Invalid Request":
        raise SafexpressAPIError(f"Invalid waybill number: {waybill_number}", status_code=400)
    return result


@_retry()
def get_pod_document(waybill_number: str, pod_base_url: str, auth_headers: dict[str, str]) -> bytes:
    """Returns the POD as raw TIFF bytes. Raises SafexpressPODNotFoundError
    (not retryable) or SafexpressAPIError (504 is retryable, other codes are not)."""
    url = f"{pod_base_url}{waybill_number}"
    try:
        resp = requests.get(url, headers=auth_headers, timeout=DEFAULT_POD_FETCH_TIMEOUT_SECONDS, verify=False)
    except requests.exceptions.RequestException as e:
        raise SafexpressAPIError(f"Request error getting POD for waybill {waybill_number}: {e}") from e

    if resp.status_code == 404:
        raise SafexpressPODNotFoundError(f"POD not found for waybill: {waybill_number}")
    if resp.status_code == 504:
        raise SafexpressAPIError(f"Gateway timeout getting POD for waybill {waybill_number}", status_code=504)
    if resp.status_code >= 400:
        raise SafexpressAPIError(
            f"HTTP error getting POD for waybill {waybill_number}: {resp.status_code}",
            status_code=resp.status_code,
        )

    result = resp.json()
    b64 = (result.get("data") or {}).get("base64String")
    if not b64 or b64 == "null":
        raise SafexpressPODNotFoundError(f"No POD data available for waybill: {waybill_number}")
    return base64.b64decode(b64)

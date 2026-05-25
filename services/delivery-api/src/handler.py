"""Delivery labels and carrier webhooks (delivery-api stack only)."""

from __future__ import annotations

import hmac
import json
import logging
import traceback
from typing import Any

from khanal_shared.config import get_settings
from khanal_shared.db.pool import get_pool
from khanal_shared.delivery import DeliveryClient
from khanal_shared.logging import configure_logging, log_json

from src.delhivery_models import (
    body_json,
    lrnum_from_track_response,
    normalize_shipment_webhook,
)
from src.delhivery_store import apply_shipment_status_webhook, upsert_tracking_snapshot

logger = logging.getLogger(__name__)
_client = DeliveryClient(api_key="stub")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _header_ci(headers: dict[str, Any] | None, name: str) -> str | None:
    if not headers:
        return None
    want = name.lower()
    for key, val in headers.items():
        if str(key).lower() == want and isinstance(val, str):
            return val
    return None


def _frappe_key_error_response(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return an error response if X-Api-Key is missing/invalid; None if OK."""
    expected = (get_settings().frappe_ingest_api_key or "").strip()
    if not expected:
        return _response(
            503,
            {
                "error": {
                    "code": "misconfigured",
                    "message": "FRAPPE_INGEST_API_KEY is not set on this Lambda",
                }
            },
        )
    got = (_header_ci(event.get("headers"), "x-api-key") or "").strip()
    if not got or not hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return _response(
            401,
            {"error": {"code": "unauthorized", "message": "Invalid or missing X-Api-Key"}},
        )
    return None


def _delhivery_webhook_secret_error_response(event: dict[str, Any]) -> dict[str, Any] | None:
    """If DELHIVERY_WEBHOOK_SECRET is set, require matching X-Delhivery-Webhook-Secret."""
    expected = (get_settings().delhivery_webhook_secret or "").strip()
    if not expected:
        return None
    got = (_header_ci(event.get("headers"), "x-delhivery-webhook-secret") or "").strip()
    if not got or not hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return _response(
            401,
            {
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing X-Delhivery-Webhook-Secret",
                }
            },
        )
    return None


def create_shipment_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    configure_logging(get_settings().log_level)
    try:
        raw = event.get("body") or "{}"
        payload = json.loads(raw) if isinstance(raw, str) else raw
        label = _client.create_label_stub(payload)
        return _response(200, label)
    except Exception:
        log_json(logger, logging.ERROR, "delivery_error", trace=traceback.format_exc())
        return _response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})


def carrier_webhook_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    configure_logging(get_settings().log_level)
    path_params = event.get("pathParameters") or {}
    carrier = path_params.get("carrier", "unknown")
    log_json(logger, logging.INFO, "carrier_webhook", carrier=carrier)
    return _response(200, {"ok": True})


def frappe_delhivery_tracking_snapshot_handler(
    event: dict[str, Any], context: object
) -> dict[str, Any]:
    """POST from Frappe after Delhivery GET /lrn/track — persists full JSON in PostgreSQL."""
    configure_logging(get_settings().log_level)
    bad = _frappe_key_error_response(event)
    if bad is not None:
        return bad
    pool = get_pool()
    if pool is None:
        return _response(
            503,
            {"error": {"code": "misconfigured", "message": "DATABASE_URL is not set"}},
        )
    body = body_json(event)
    track = body.get("delhivery_track_response")
    if not isinstance(track, dict):
        return _response(
            400,
            {
                "error": {
                    "code": "invalid_body",
                    "message": "Expected JSON with delhivery_track_response object",
                }
            },
        )
    lrnum = body.get("lrnum") if isinstance(body.get("lrnum"), str) else None
    if lrnum:
        lrnum = lrnum.strip()
    if not lrnum:
        lrnum = lrnum_from_track_response(track)
    if not lrnum:
        return _response(
            400,
            {
                "error": {
                    "code": "invalid_body",
                    "message": "Could not determine lrnum (pass lrnum or Delhivery data.lrnum)",
                }
            },
        )
    try:
        with pool.connection() as conn:
            upsert_tracking_snapshot(conn, lrnum, track)
        log_json(logger, logging.INFO, "delhivery_tracking_snapshot_stored", lrnum=lrnum)
        return _response(200, {"ok": True, "lrnum": lrnum})
    except Exception:
        log_json(logger, logging.ERROR, "delhivery_tracking_snapshot_error", trace=traceback.format_exc())
        return _response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})


def delhivery_shipment_status_webhook_handler(
    event: dict[str, Any], context: object
) -> dict[str, Any]:
    """POST from Delhivery — updates delhivery_status (e.g. DELIVERED) by lrnum."""
    configure_logging(get_settings().log_level)
    bad = _delhivery_webhook_secret_error_response(event)
    if bad is not None:
        return bad
    pool = get_pool()
    if pool is None:
        return _response(
            503,
            {"error": {"code": "misconfigured", "message": "DATABASE_URL is not set"}},
        )
    payload = body_json(event)
    if not payload:
        return _response(400, {"error": {"code": "invalid_body", "message": "Expected JSON body"}})
    fields = normalize_shipment_webhook(payload)
    if fields is None:
        return _response(400, {"error": {"code": "invalid_body", "message": "Missing lrnum"}})
    try:
        with pool.connection() as conn:
            apply_shipment_status_webhook(conn, fields)
        log_json(
            logger,
            logging.INFO,
            "delhivery_shipment_webhook",
            lrnum=fields["lrnum"],
            status=fields.get("delhivery_status"),
        )
        return _response(200, {"ok": True, "lrnum": fields["lrnum"]})
    except Exception:
        log_json(logger, logging.ERROR, "delhivery_shipment_webhook_error", trace=traceback.format_exc())
        return _response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})

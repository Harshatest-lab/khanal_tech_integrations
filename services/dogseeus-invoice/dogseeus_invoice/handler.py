"""Dogseeus_Invoice — separate Serverless stack (dogseeus-invoice)."""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from khanal_shared.config import get_settings
from khanal_shared.logging import configure_logging, log_json

logger = logging.getLogger(__name__)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def health_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """GET /v1/dogseeus/health — simple liveness check for this service."""
    configure_logging(get_settings().log_level)
    return _response(200, {"service": "dogseeus-invoice", "status": "ok"})


def invoice_handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """POST /v1/dogseeus/invoices — extend with real invoicing."""
    configure_logging(get_settings().log_level)
    try:
        raw = event.get("body") or "{}"
        payload = json.loads(raw) if isinstance(raw, str) else raw
        log_json(
            logger,
            logging.INFO,
            "dogseeus_invoice",
            keys=list(payload.keys()) if isinstance(payload, dict) else [],
        )
        return _response(
            200,
            {
                "service": "Dogseeus_Invoice",
                "status": "stub",
                "invoice": {"id": "inv_stub_001", "currency": "USD", "total": "0.00"},
            },
        )
    except Exception:
        log_json(logger, logging.ERROR, "dogseeus_invoice_error", trace=traceback.format_exc())
        return _response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})

"""Lambda handler — Delhivery calls this when shipment status changes.

Route: POST /v1/webhooks/delhivery/shipment-status
Auth:  Optional X-Delhivery-Webhook-Secret header (set DELHIVERY_WEBHOOK_SECRET env var to enable)
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from khanal_shared.config import get_settings
from khanal_shared.db.pool import get_pool
from khanal_shared.logging import configure_logging, log_json

from src.common import delhivery_secret_error_response, response
from src.delhivery_models import body_json, normalize_shipment_webhook
from src.delhivery_store import apply_shipment_status_webhook

logger = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Update delhivery_status in DB when Delhivery pushes a status change."""
    configure_logging(get_settings().log_level)

    bad = delhivery_secret_error_response(event)
    if bad is not None:
        return bad

    pool = get_pool()
    if pool is None:
        return response(503, {"error": {"code": "misconfigured", "message": "DATABASE_URL is not set"}})

    payload = body_json(event)
    if not payload:
        return response(400, {"error": {"code": "invalid_body", "message": "Expected JSON body"}})

    fields = normalize_shipment_webhook(payload)
    if fields is None:
        return response(400, {"error": {"code": "invalid_body", "message": "Missing lrnum"}})

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
        return response(200, {"ok": True, "lrnum": fields["lrnum"]})
    except Exception:
        log_json(logger, logging.ERROR, "delhivery_webhook_error", trace=traceback.format_exc())
        return response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})

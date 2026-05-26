"""Lambda handler — Delhivery calls this when a POD (Proof of Delivery) is ready.

Route: POST /v1/webhooks/delhivery/pod
Auth:  Optional X-Delhivery-Webhook-Secret header (set DELHIVERY_WEBHOOK_SECRET env var)

Delhivery may push multiple PODs per LR (e.g. revised PODs from the audit team).
Every push is stored as a new row in delhivery_lr_pod — the table is append-only.

Expected payload:
    {
        "waybill":  "1234567890",
        "EPOD":     "<base64-encoded POD string>",
        "orderID":  "<optional order id>",
        "lrnum":    "220256789"
    }
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from khanal_shared.config import get_settings
from khanal_shared.db.pool import get_pool
from khanal_shared.logging import configure_logging, log_json

from src.common import delhivery_secret_error_response, response
from src.delhivery_models import body_json, normalize_pod_webhook
from src.delhivery_store import insert_pod_event

logger = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Store each POD push from Delhivery into delhivery_lr_pod."""
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

    fields = normalize_pod_webhook(payload)
    if fields is None:
        return response(
            400,
            {"error": {"code": "invalid_body", "message": "Missing lrnum and waybill — cannot identify shipment"}},
        )

    try:
        with pool.connection() as conn:
            insert_pod_event(conn, fields)
        log_json(
            logger,
            logging.INFO,
            "delhivery_pod_received",
            lrnum=fields["lrnum"],
            waybill=fields.get("waybill"),
            order_id=fields.get("order_id"),
            has_epod=bool(fields.get("epod_b64")),
        )
        return response(200, {"ok": True, "lrnum": fields["lrnum"]})
    except Exception:
        log_json(logger, logging.ERROR, "delhivery_pod_webhook_error", trace=traceback.format_exc())
        return response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})

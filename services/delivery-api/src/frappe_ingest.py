"""Lambda handler — Frappe calls this after Delhivery GET /lrn/track.

Route: POST /v1/integrations/frappe/delhivery-tracking-snapshot
Auth:  X-Api-Key header must match FRAPPE_INGEST_API_KEY env var
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from khanal_shared.config import get_settings
from khanal_shared.db.pool import get_pool
from khanal_shared.logging import configure_logging, log_json

from src.common import frappe_key_error_response, response
from src.delhivery_models import body_json, lrnum_from_track_response
from src.delhivery_store import upsert_tracking_snapshot

logger = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Store Delhivery track JSON sent by Frappe into PostgreSQL."""
    configure_logging(get_settings().log_level)

    bad = frappe_key_error_response(event)
    if bad is not None:
        return bad

    pool = get_pool()
    if pool is None:
        return response(503, {"error": {"code": "misconfigured", "message": "DATABASE_URL is not set"}})

    body = body_json(event)
    track = body.get("delhivery_track_response")
    if not isinstance(track, dict):
        return response(
            400,
            {"error": {"code": "invalid_body", "message": "Expected JSON with delhivery_track_response object"}},
        )

    lrnum = body.get("lrnum") if isinstance(body.get("lrnum"), str) else None
    if lrnum:
        lrnum = lrnum.strip()
    if not lrnum:
        lrnum = lrnum_from_track_response(track)
    if not lrnum:
        return response(
            400,
            {"error": {"code": "invalid_body", "message": "Could not determine lrnum"}},
        )

    try:
        with pool.connection() as conn:
            upsert_tracking_snapshot(conn, lrnum, track)
        log_json(logger, logging.INFO, "delhivery_tracking_snapshot_stored", lrnum=lrnum)
        return response(200, {"ok": True, "lrnum": lrnum})
    except Exception:
        log_json(logger, logging.ERROR, "frappe_ingest_error", trace=traceback.format_exc())
        return response(500, {"error": {"code": "internal_error", "message": "Unexpected error"}})

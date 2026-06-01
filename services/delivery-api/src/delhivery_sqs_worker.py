"""Background worker: SQS → same Delhivery persistence as the old synchronous handlers."""

from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

from khanal_shared.config import get_settings
from khanal_shared.db.pool import ensure_warmed_for_lambda, get_pool
from khanal_shared.logging import configure_logging, log_json

from src.common import delhivery_secret_error_response
from src.delhivery_models import normalize_pod_webhook, normalize_shipment_webhook
from src.delhivery_store import apply_shipment_status_webhook, insert_pod_event

logger = logging.getLogger(__name__)


def _process_one_record(pool: Any, record: dict[str, Any]) -> None:
    envelope = json.loads(record["body"])
    kind = envelope.get("kind")
    headers = envelope.get("headers") or {}
    payload = envelope.get("body")
    if not isinstance(payload, dict):
        log_json(logger, logging.WARNING, "delhivery_sqs_skip", reason="invalid_envelope_body")
        return

    synthetic_event: dict[str, Any] = {"headers": headers}
    bad = delhivery_secret_error_response(synthetic_event)
    if bad is not None:
        log_json(logger, logging.WARNING, "delhivery_sqs_drop_unauthorized", kind=kind)
        return

    if kind == "shipment_status":
        fields = normalize_shipment_webhook(payload)
        if fields is None:
            log_json(logger, logging.WARNING, "delhivery_sqs_skip", reason="missing_lrnum")
            return
        with pool.connection() as conn:
            apply_shipment_status_webhook(conn, fields)
        log_json(
            logger,
            logging.INFO,
            "delhivery_shipment_webhook",
            lrnum=fields["lrnum"],
            status=fields.get("delhivery_status"),
        )
        return

    if kind == "pod":
        fields = normalize_pod_webhook(payload)
        if fields is None:
            log_json(logger, logging.WARNING, "delhivery_sqs_skip", reason="missing_pod_identifiers")
            return
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
        return

    log_json(logger, logging.WARNING, "delhivery_sqs_unknown_kind", kind=kind)


def handler(event: dict[str, Any], context: object) -> None:
    configure_logging(get_settings().log_level)
    if os.environ.get("AWS_EXECUTION_ENV"):
        ensure_warmed_for_lambda()

    pool = get_pool()
    if pool is None:
        raise RuntimeError("DATABASE_URL is not set")

    records = event.get("Records") or []
    for record in records:
        try:
            _process_one_record(pool, record)
        except Exception:
            log_json(logger, logging.ERROR, "delhivery_sqs_worker_error", trace=traceback.format_exc())
            raise

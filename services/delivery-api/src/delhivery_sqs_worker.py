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

from src.common import delhivery_secret_error_response, header_ci
from src.delhivery_models import normalize_pod_webhook, normalize_shipment_webhook
from src.delhivery_store import apply_pod_webhook, apply_shipment_status_webhook
from src.pod_epod_s3 import maybe_upload_epod_clear_b64

logger = logging.getLogger(__name__)


def _skip_webhook_secret_check() -> bool:
    v = (os.environ.get("DELHIVERY_SKIP_WEBHOOK_SECRET") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _sqs_attr_str(message_attributes: dict[str, Any] | None, name: str) -> str | None:
    if not message_attributes:
        return None
    entry = message_attributes.get(name)
    if not isinstance(entry, dict):
        return None
    val = entry.get("stringValue")
    if isinstance(val, str):
        return val
    val = entry.get("StringValue")
    return val if isinstance(val, str) else None


def _process_one_record(pool: Any, record: dict[str, Any]) -> None:
    body_str = record.get("body") or ""
    message_attributes = record.get("messageAttributes")

    try:
        parsed: Any = json.loads(body_str)
    except json.JSONDecodeError:
        log_json(logger, logging.WARNING, "delhivery_sqs_skip", reason="invalid_json_body")
        return

    # Legacy envelope: {"kind","headers","body"} (old ingest Lambda).
    if (
        isinstance(parsed, dict)
        and "kind" in parsed
        and "body" in parsed
        and isinstance(parsed.get("body"), dict)
    ):
        kind = parsed.get("kind")
        if isinstance(kind, str):
            kln = kind.strip().lower()
            if kln == "shipment_status":
                kind = "shipment_status"
            elif kln == "pod":
                kind = "pod"
        headers = dict(parsed.get("headers") or {})
        # Same as non-legacy path: secret may live only on SQS message attributes.
        sec = _sqs_attr_str(message_attributes, "webhookSecret")
        if sec:
            headers["x-delhivery-webhook-secret"] = sec
        payload = parsed["body"]
        synthetic_event: dict[str, Any] = {"headers": headers}
    else:
        if not isinstance(parsed, dict):
            log_json(logger, logging.WARNING, "delhivery_sqs_skip", reason="expected_object_json")
            return
        kind_raw = (_sqs_attr_str(message_attributes, "webhookKind") or "").strip()
        kind_lower = kind_raw.lower()
        if kind_lower == "shipment_status":
            kind = "shipment_status"
        elif kind_lower == "pod":
            kind = "pod"
        else:
            log_json(logger, logging.WARNING, "delhivery_sqs_unknown_kind", kind=kind_raw or None)
            return
        payload = parsed
        sec = _sqs_attr_str(message_attributes, "webhookSecret")
        headers: dict[str, Any] = {}
        if sec:
            headers["x-delhivery-webhook-secret"] = sec
        synthetic_event = {"headers": headers}

    bad = None if _skip_webhook_secret_check() else delhivery_secret_error_response(synthetic_event)
    if bad is not None:
        got = (header_ci(synthetic_event.get("headers"), "x-delhivery-webhook-secret") or "").strip()
        exp = (os.environ.get("DELHIVERY_WEBHOOK_SECRET") or "").strip()
        attr_keys = sorted(message_attributes.keys()) if isinstance(message_attributes, dict) else []
        log_json(
            logger,
            logging.WARNING,
            "delhivery_sqs_drop_unauthorized",
            kind=kind,
            got_len=len(got),
            expected_len=len(exp),
            message_attribute_keys=attr_keys,
        )
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
        # Optional: decode EPOD → object in S3; DB stores HTTPS ``pod_url`` (not ``epod_b64``).
        maybe_upload_epod_clear_b64(fields)
        with pool.connection() as conn:
            new_pod_id = apply_pod_webhook(conn, fields)
        if new_pod_id is None:
            log_json(
                logger,
                logging.WARNING,
                "delhivery_pod_skipped",
                lrnum=fields.get("lrnum"),
                waybill=fields.get("waybill"),
                reason="no_matching_delhivery_lr",
            )
        else:
            log_json(
                logger,
                logging.INFO,
                "delhivery_pod_received",
                lrnum=fields.get("lrnum"),
                waybill=fields.get("waybill"),
                order_id=fields.get("order_id"),
                pod_id=new_pod_id,
                pod_format=fields.get("pod_format"),
                has_epod=bool(fields.get("epod_b64")),
                has_pod_url=bool(fields.get("pod_url")),
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

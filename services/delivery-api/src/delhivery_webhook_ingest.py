"""Fast Delhivery HTTP ingest: optional secret check, enqueue to SQS, return JSON.

No VPC, no shared layer — only stdlib + boto3 + pure models (keeps cold start small).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import hmac
from typing import Any

import boto3

from src.delhivery_models import body_json, normalize_pod_webhook, normalize_shipment_webhook

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Build the client at import time so work runs during Lambda INIT, not inside
# START→END Duration (lazy client in-handler was inflating Duration ~1s on cold).
sqs_client: Any = boto3.client("sqs")


def _api_response(status: int, body: dict[str, Any]) -> dict[str, Any]:
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


def _secret_error_response(event: dict[str, Any]) -> dict[str, Any] | None:
    """401 if secret is configured and header does not match; None if OK."""
    expected = (os.getenv("DELHIVERY_WEBHOOK_SECRET") or "").strip()
    if not expected:
        return None
    got = (_header_ci(event.get("headers"), "x-delhivery-webhook-secret") or "").strip()
    if not got or not hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8")):
        return _api_response(
            401,
            {"error": {"code": "unauthorized", "message": "Invalid or missing X-Delhivery-Webhook-Secret"}},
        )
    return None


def _kind_from_event(event: dict[str, Any]) -> str | None:
    path = event.get("rawPath") or ""
    if path.rstrip("/").endswith("/shipment-status"):
        return "shipment_status"
    if path.rstrip("/").endswith("/pod"):
        return "pod"
    http = (event.get("requestContext") or {}).get("http") or {}
    p = (http.get("path") or "").rstrip("/")
    if p.endswith("/shipment-status"):
        return "shipment_status"
    if p.endswith("/pod"):
        return "pod"
    return None


def _decode_body(event: dict[str, Any]) -> dict[str, Any]:
    """Return event copy suitable for body_json (decoded body string)."""
    raw = event.get("body") or "{}"
    if isinstance(raw, dict):
        return event
    if not isinstance(raw, str):
        return {**event, "body": "{}"}
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raw = "{}"
    return {**event, "body": raw, "isBase64Encoded": False}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    bad = _secret_error_response(event)
    if bad is not None:
        return bad

    kind = _kind_from_event(event)
    if kind is None:
        return _api_response(404, {"error": {"code": "not_found", "message": "Unknown webhook path"}})

    payload = body_json(_decode_body(event))
    if not payload:
        return _api_response(400, {"error": {"code": "invalid_body", "message": "Expected JSON body"}})

    if kind == "shipment_status":
        fields = normalize_shipment_webhook(payload)
        if fields is None:
            return _api_response(400, {"error": {"code": "invalid_body", "message": "Missing lrnum"}})
        lrnum = fields["lrnum"]
    else:
        fields = normalize_pod_webhook(payload)
        if fields is None:
            return _api_response(
                400,
                {"error": {"code": "invalid_body", "message": "Missing lrnum and waybill — cannot identify shipment"}},
            )
        lrnum = fields["lrnum"]

    queue_url = (os.getenv("DELHIVERY_WEBHOOK_QUEUE_URL") or "").strip()
    if not queue_url:
        logger.error("DELHIVERY_WEBHOOK_QUEUE_URL is not set")
        return _api_response(503, {"error": {"code": "misconfigured", "message": "Queue URL is not configured"}})

    envelope = {
        "kind": kind,
        "headers": dict(event.get("headers") or {}),
        "body": payload,
    }

    try:
        sqs_client.send_message(QueueUrl=queue_url, MessageBody=json.dumps(envelope))
    except Exception as exc:
        logger.exception("SQS send_message failed: %s", exc)
        return _api_response(503, {"error": {"code": "queue_error", "message": "Failed to enqueue webhook"}})

    return _api_response(200, {"ok": True, "lrnum": lrnum})

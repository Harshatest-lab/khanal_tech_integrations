"""Decode Delhivery EPOD (base64) and upload bytes to S3 for durable ``pod_url`` storage."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import traceback
import uuid
from typing import Any
from urllib.parse import quote

from khanal_shared.logging import log_json

logger = logging.getLogger(__name__)

# PDF, JPEG, PNG magic bytes (Delhivery EPOD is often PDF; sometimes image).
_SNIFF: list[tuple[bytes, str, str]] = [
    (b"%PDF", "application/pdf", "pdf"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
]


def decode_epod_bytes(epod_b64: str) -> bytes:
    """Decode base64 EPOD string to raw bytes (lenient padding)."""
    raw = epod_b64.strip()
    if not raw:
        raise ValueError("empty EPOD")
    pad = (-len(raw)) % 4
    if pad:
        raw += "=" * pad
    try:
        return base64.b64decode(raw, validate=False)
    except binascii.Error as e:
        raise ValueError("invalid base64 EPOD") from e


def sniff_content_type_and_ext(data: bytes) -> tuple[str, str]:
    """Return ``(content_type, file_extension)`` from leading bytes."""
    for prefix, ctype, ext in _SNIFF:
        if data.startswith(prefix):
            return ctype, ext
    return "application/octet-stream", "bin"


def _sanitize_key_segment(s: str, max_len: int = 96) -> str:
    t = (s or "").strip()
    if not t:
        return "unknown"
    out = re.sub(r"[^a-zA-Z0-9._-]+", "_", t)
    return out[:max_len] if len(out) > max_len else out


def s3_object_https_url(*, bucket: str, region: str, key: str) -> str:
    """Virtual-hosted–style object URL with path segments encoded for HTTP."""
    encoded = "/".join(quote(part, safe="") for part in key.split("/"))
    return f"https://{bucket}.s3.{region}.amazonaws.com/{encoded}"


def upload_epod_to_s3(
    *,
    bucket: str,
    key_prefix: str,
    lr_or_waybill: str,
    epod_b64: str,
    s3_client: Any | None = None,
) -> str:
    """Decode ``epod_b64``, ``PutObject`` under ``key_prefix``, return HTTPS object URL."""
    data = decode_epod_bytes(epod_b64)
    if not data:
        raise ValueError("decoded EPOD is empty")

    ctype, ext = sniff_content_type_and_ext(data)
    prefix = (key_prefix or "delhivery-pod").strip().strip("/")
    seg = _sanitize_key_segment(lr_or_waybill)
    key = f"{prefix}/{seg}/{uuid.uuid4()}.{ext}"

    region = (os.environ.get("AWS_REGION") or "us-east-1").strip()
    if s3_client is not None:
        client = s3_client
    else:
        import boto3
        from botocore.config import Config

        # Avoid hanging until Lambda timeout when VPC↔S3 routing is wrong (fail fast + clear error in logs).
        client = boto3.client(
            "s3",
            region_name=region,
            config=Config(connect_timeout=8, read_timeout=25, retries={"max_attempts": 2, "mode": "standard"}),
        )
    log_json(
        logger,
        logging.INFO,
        "delhivery_pod_s3_put_start",
        bucket=bucket,
        key=key,
        bytes=len(data),
        content_type=ctype,
    )
    client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=ctype)

    return s3_object_https_url(bucket=bucket, region=region, key=key)


def maybe_upload_epod_clear_b64(fields: dict[str, Any]) -> None:
    """If ``POD_S3_BUCKET`` is set and ``epod_b64`` is present, upload to S3 and clear b64.

    Mutates ``fields`` in place: sets ``pod_url`` to the durable object URL, sets
    ``epod_b64`` to ``None``, and ``pod_format`` to ``url``. No-op when bucket env is unset.

    On upload/decode failure, logs and re-raises so SQS can retry / DLQ.
    """
    bucket = (os.environ.get("POD_S3_BUCKET") or "").strip()
    if not bucket:
        return

    epod = fields.get("epod_b64")
    if not epod or not isinstance(epod, str) or not epod.strip():
        return

    lr = (fields.get("lrnum") or fields.get("waybill") or "").strip()
    if isinstance(lr, str) and lr:
        lr_key = lr
    else:
        lr_key = "unknown"

    prefix = (os.environ.get("POD_S3_PREFIX") or "delhivery-pod").strip()
    try:
        url = upload_epod_to_s3(
            bucket=bucket,
            key_prefix=prefix,
            lr_or_waybill=lr_key,
            epod_b64=epod,
        )
    except Exception:
        log_json(
            logger,
            logging.ERROR,
            "delhivery_pod_s3_upload_failed",
            lrnum=fields.get("lrnum"),
            waybill=fields.get("waybill"),
            trace=traceback.format_exc(),
        )
        raise

    fields["pod_url"] = url
    fields["epod_b64"] = None
    fields["pod_format"] = "url"

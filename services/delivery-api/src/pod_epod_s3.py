"""Decode Delhivery EPOD (base64) or fetch external ``pod_url``; upload bytes to S3 for durable storage."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import traceback
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from khanal_shared.logging import log_json

logger = logging.getLogger(__name__)

# Delhivery EPOD PDFs are usually small; cap download size to avoid Lambda OOM.
_MAX_POD_BYTES = 15 * 1024 * 1024
_DEFAULT_FETCH_TIMEOUT_SEC = 30.0

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


def _s3_client(region: str, s3_client: Any | None) -> Any:
    if s3_client is not None:
        return s3_client
    import boto3
    from botocore.config import Config

    # Avoid hanging until Lambda timeout when VPC↔S3 routing is wrong (fail fast + clear error in logs).
    return boto3.client(
        "s3",
        region_name=region,
        config=Config(connect_timeout=8, read_timeout=25, retries={"max_attempts": 2, "mode": "standard"}),
    )


def _ext_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    ct = content_type.split(";", 1)[0].strip().lower()
    return {
        "application/pdf": "pdf",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
    }.get(ct)


def is_our_s3_pod_url(url: str, bucket: str) -> bool:
    """True when ``url`` already points at an object in ``bucket`` (skip re-upload)."""
    u = (url or "").strip()
    b = (bucket or "").strip().lower()
    if not u or not b:
        return False
    host = urlparse(u).netloc.lower()
    if host == f"{b}.s3.amazonaws.com" or host.startswith(f"{b}.s3."):
        return True
    return f"s3.amazonaws.com/{b}/" in u.lower()


def fetch_pod_url_bytes(
    url: str,
    *,
    timeout_sec: float | None = None,
    max_bytes: int = _MAX_POD_BYTES,
    opener: Any | None = None,
) -> bytes:
    """HTTP GET a Delhivery (or other) pre-signed ``pod_url``; return response body bytes."""
    target = (url or "").strip()
    if not target:
        raise ValueError("empty pod_url")
    timeout = timeout_sec if timeout_sec is not None else _DEFAULT_FETCH_TIMEOUT_SEC
    req = Request(target, headers={"Accept": "*/*"}, method="GET")
    open_fn = opener or urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            if status < 200 or status >= 300:
                raise ValueError(f"pod_url fetch failed with HTTP {status}")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(65536)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise ValueError(f"pod_url response exceeds {max_bytes} bytes")
                chunks.append(block)
    except HTTPError as e:
        raise ValueError(f"pod_url fetch failed with HTTP {e.code}") from e
    except URLError as e:
        raise ValueError(f"pod_url fetch failed: {e.reason}") from e

    data = b"".join(chunks)
    if not data:
        raise ValueError("pod_url response is empty")
    return data


def upload_pod_bytes_to_s3(
    *,
    bucket: str,
    key_prefix: str,
    lr_or_waybill: str,
    data: bytes,
    s3_client: Any | None = None,
    content_type: str | None = None,
    ext: str | None = None,
) -> str:
    """``PutObject`` under ``key_prefix``; return virtual-hosted HTTPS object URL."""
    if not data:
        raise ValueError("empty POD bytes")

    sniffed_ctype, sniffed_ext = sniff_content_type_and_ext(data)
    ctype = (content_type or sniffed_ctype).strip() or sniffed_ctype
    file_ext = ext or _ext_from_content_type(content_type) or sniffed_ext

    prefix = (key_prefix or "delhivery-pod").strip().strip("/")
    seg = _sanitize_key_segment(lr_or_waybill)
    key = f"{prefix}/{seg}/{uuid.uuid4()}.{file_ext}"

    region = (os.environ.get("AWS_REGION") or "us-east-1").strip()
    client = _s3_client(region, s3_client)
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
    return upload_pod_bytes_to_s3(
        bucket=bucket,
        key_prefix=key_prefix,
        lr_or_waybill=lr_or_waybill,
        data=data,
        s3_client=s3_client,
    )


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


def maybe_mirror_external_pod_url(fields: dict[str, Any]) -> None:
    """When ``pod_url`` is an external (e.g. Delhivery pre-signed) link, fetch and store in our S3.

    Mutates ``fields`` in place: replaces ``pod_url`` with the durable object URL and sets
    ``pod_format`` to ``url``. No-op when bucket env is unset, ``epod_b64`` is still set,
    ``pod_url`` is missing, or ``pod_url`` already targets ``POD_S3_BUCKET``.

    On fetch/upload failure, logs and re-raises so SQS can retry / DLQ.
    """
    bucket = (os.environ.get("POD_S3_BUCKET") or "").strip()
    if not bucket:
        return

    epod = fields.get("epod_b64")
    if epod and isinstance(epod, str) and epod.strip():
        return

    pod_url = fields.get("pod_url")
    if not pod_url or not isinstance(pod_url, str) or not pod_url.strip():
        return

    external = pod_url.strip()
    if is_our_s3_pod_url(external, bucket):
        return

    lr = (fields.get("lrnum") or fields.get("waybill") or "").strip()
    lr_key = lr if isinstance(lr, str) and lr else "unknown"
    prefix = (os.environ.get("POD_S3_PREFIX") or "delhivery-pod").strip()

    log_json(
        logger,
        logging.INFO,
        "delhivery_pod_url_mirror_start",
        lrnum=fields.get("lrnum"),
        waybill=fields.get("waybill"),
        source_host=urlparse(external).netloc,
    )
    try:
        data = fetch_pod_url_bytes(external)
        url = upload_pod_bytes_to_s3(
            bucket=bucket,
            key_prefix=prefix,
            lr_or_waybill=lr_key,
            data=data,
        )
    except Exception:
        log_json(
            logger,
            logging.ERROR,
            "delhivery_pod_url_mirror_failed",
            lrnum=fields.get("lrnum"),
            waybill=fields.get("waybill"),
            trace=traceback.format_exc(),
        )
        raise

    fields["pod_url"] = url
    fields["pod_format"] = "url"

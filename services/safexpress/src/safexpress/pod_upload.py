"""Upload a downloaded POD (TIFF) to the existing SafeExpress POD bucket.

Kept on the existing us-west-1 API Gateway + S3 bucket by explicit decision
(2026-08-24) — continuity with PODs already stored under this URL scheme,
not migrated to ap-south-1.
"""

from __future__ import annotations

import logging
import time

import requests

from .config import DEFAULT_UPLOAD_TIMEOUT_SECONDS, POD_S3_BUCKET_URL, POD_S3_UPLOAD_URL
from .exceptions import SafexpressFileUploadError

logger = logging.getLogger(__name__)


def upload_pod(pod_data: bytes, series_name: str, doc_number: int, waybill_number: str) -> str:
    """Uploads `pod_data` and returns its public S3 URL."""
    filename = f"{series_name or 'UNKNOWN'}_{doc_number}_{int(time.time())}.tiff"
    upload_url = f"{POD_S3_UPLOAD_URL}{filename}"
    try:
        resp = requests.put(
            upload_url, headers={"Content-Type": "image/tiff"}, data=pod_data,
            timeout=DEFAULT_UPLOAD_TIMEOUT_SECONDS, verify=False,
        )
    except requests.exceptions.RequestException as e:
        raise SafexpressFileUploadError(f"S3 upload failed for waybill {waybill_number}: {e}") from e

    if resp.status_code not in (200, 201):
        raise SafexpressFileUploadError(
            f"S3 upload failed for waybill {waybill_number}: {resp.status_code} - {resp.text[:300]}"
        )
    pod_url = f"{POD_S3_BUCKET_URL}{filename}"
    logger.info("Uploaded POD to S3: %s", pod_url)
    return pod_url

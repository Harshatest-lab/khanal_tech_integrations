"""SP-API Reports API for Amazon Flex: create -> poll -> download (gunzip
when flagged). Ported from amazon-us-b2c's amazon_us/reports_api.py, same
report types khanal-oms has already proven return real PRCJ-carrying rows
against this seller account/marketplace.

PRCJ filtering (fulfillment_center == "PRCJ" vs real-FBA centres) happens in
flex_fetch.py, not here -- this module only fetches the raw report content.
"""

from __future__ import annotations

import gzip
import logging
import time

import requests

from unicommerce import amazon_sp_api as sp_api

logger = logging.getLogger(__name__)

_REPORTS_BASE = "/reports/2021-06-30"

ORDERS_REPORT_TYPE = "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL"
# Carries fulfillment_center -- the field the PRCJ vs real-FBA discriminator
# reads (see docs/superpowers/specs/2026-08-20-amazon-flex-sap-pipeline-design.md).
FLEX_SHIPMENTS_REPORT_TYPE = "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL"


def create_report(report_type: str, marketplace_id: str, data_start_time: str, data_end_time: str) -> str:
    """Request a report; returns reportId. Times are UTC ISO 8601.
    Windows must stay <= ~31 days or Amazon FATALs the report."""
    body = {
        "reportType": report_type,
        "marketplaceIds": [marketplace_id],
        "dataStartTime": data_start_time,
        "dataEndTime": data_end_time,
    }
    resp = sp_api.post(f"{_REPORTS_BASE}/reports", body)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"create_report failed: {resp.status_code} {resp.text[:300]}")
    return resp.json().get("reportId")


def poll_report(report_id: str, poll_interval: int = 30, max_wait: int = 840) -> str:
    """Poll until DONE; returns reportDocumentId. Raises on FATAL/CANCELLED/timeout."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        resp = sp_api.get(f"{_REPORTS_BASE}/reports/{report_id}")
        if resp.status_code != 200:
            raise RuntimeError(f"poll_report failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        status = data.get("processingStatus", "")
        logger.info("[flex-reports] %s status=%s", report_id, status)
        if status == "DONE":
            return data.get("reportDocumentId")
        if status in ("FATAL", "CANCELLED"):
            raise RuntimeError(f"Report {report_id} ended with status={status}")
        time.sleep(poll_interval)
    raise TimeoutError(f"Report {report_id} not ready after {max_wait}s")


def cancel_report(report_id: str) -> bool:
    """Cancel a still-queued/in-progress report. Only valid while status is
    IN_QUEUE or IN_PROGRESS. Returns True on success."""
    resp = sp_api.delete(f"{_REPORTS_BASE}/reports/{report_id}")
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"cancel_report failed: {resp.status_code} {resp.text[:300]}")
    return True


def download_report(report_document_id: str) -> str:
    """Fetch presigned URL and return report content (gunzipped if needed)."""
    resp = sp_api.get(f"{_REPORTS_BASE}/documents/{report_document_id}")
    if resp.status_code != 200:
        raise RuntimeError(f"download_report metadata failed: {resp.status_code} {resp.text[:200]}")
    doc_info = resp.json()
    url = doc_info.get("url")
    if not url:
        raise ValueError(f"No download URL for report document {report_document_id}")

    content_resp = requests.get(url, timeout=120)
    content_resp.raise_for_status()

    if (doc_info.get("compressionAlgorithm") or "").upper() == "GZIP":
        return gzip.decompress(content_resp.content).decode("utf-8", errors="replace")
    return content_resp.text

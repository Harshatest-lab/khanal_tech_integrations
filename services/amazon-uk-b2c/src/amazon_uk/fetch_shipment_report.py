"""Fetch the Amazon-fulfilled shipments report (Reports API).

This is the only source with the actual ship date, carrier and tracking
number — Orders/GetOrderItems never have them. Flow: create the report,
poll until DONE, download it, parse the tab-separated rows.
"""

import csv
import gzip
import io
import time

import requests

from amazon_uk import sp_api

REPORT_TYPE = "GET_AMAZON_FULFILLED_SHIPMENTS_DATA_GENERAL"


def fetch(data_start: str, data_end: str,
          poll_interval_s: int = 30, max_polls: int = 20) -> list[dict]:
    created = sp_api.request("POST", "/reports/2021-06-30/reports", json_body={
        "reportType": REPORT_TYPE,
        "marketplaceIds": [sp_api.MARKETPLACE_ID],
        "dataStartTime": data_start,
        "dataEndTime": data_end,
    })
    report_id = created["reportId"]

    document_id = None
    for _ in range(max_polls):
        status = sp_api.request("GET", f"/reports/2021-06-30/reports/{report_id}")
        processing_status = status["processingStatus"]

        if processing_status == "DONE":
            document_id = status["reportDocumentId"]
            break
        if processing_status == "CANCELLED":
            return []  # Amazon cancels this report type when there's no data in range
        if processing_status == "FATAL":
            raise RuntimeError(f"Report {report_id} failed: {processing_status}")

        time.sleep(poll_interval_s)

    if document_id is None:
        raise RuntimeError(f"Report {report_id} still processing after {max_polls} polls")

    doc = sp_api.request("GET", f"/reports/2021-06-30/documents/{document_id}")
    raw = requests.get(doc["url"], timeout=60).content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)

    text = raw.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))

"""Configuration constants for the SafeExpress integration.

Public endpoints and non-secret defaults only — real credentials live in
SSM (see credentials.py), never here.
"""

from __future__ import annotations

# SafeExpress's own public API endpoints (not secrets).
TRACKING_API_URL = "https://webs.safexpress.com:8443/SafexWaybillTracking/webresources/tracking/all"

# POD upload target: kept on the existing us-west-1 bucket/API Gateway by
# explicit decision (2026-08-24) — not migrated to ap-south-1, for
# continuity with PODs already stored there under this URL scheme.
POD_S3_UPLOAD_URL = "https://tg31l9q380.execute-api.us-west-1.amazonaws.com/dev/khanalfoods-fileupload-bucket/"
POD_S3_BUCKET_URL = "https://khanalfoods-fileupload-bucket.s3.us-west-1.amazonaws.com/"

# Invoice series prefixes SafeExpress invoice numbers are formatted with
# ("KAIN24/12345" etc). Refreshed 2026-08-28 from real prod HANA data
# (every NNM1 series real OINV rows actually use, ObjectCode='13') once
# the series-name join was fixed (see SAP_OBJECT_CODE_INVOICES below) —
# now mostly a safety net for a future series year the join hasn't seen
# yet, not the common path.
INVOICE_SERIES_PREFIXES: list[str] = [
    "AR2022", "KAIN22", "MHIN22", "HRIN22",
    "AR2023", "KAIN23", "MHIN23", "INTSER22", "INTSER23", "KAB2C23", "KASER23", "INTL23",
    "KAIN24", "KAB2C24", "KASER24", "INTL24",
    "KAIN25", "INTSER25", "KAB2C25", "HRB2C25", "MHB2C25", "INTL25", "TNB2C25",
    "INTSER26", "KAIN26", "TNB2C26", "INTL26", "MHB2C26", "HRB2C26", "KAB2C26", "AR2026",
    "Manual",
]

# SAP NNM1 (numbering series master) ObjectCode for A/R Invoices — used to
# scope the OINV->NNM1 series_name join (hana_candidates.py) to invoice
# series only, avoiding any ambiguity if a Series number is ever reused by
# a different document type. Verified 2026-08-28 against real prod data.
SAP_OBJECT_CODE_INVOICES = "13"

# SAP TransportationCode values, ported from
# khanal_tech_integrations/utils/safexpress/automated_invoice_attach.py's
# TransportationCodeDict — SafeExpress is code 1. DSR Logistics is not in
# SAP's TransportationCode list at all; it shares code 1 with SafeExpress
# in practice (per the legacy transporter_type exclusion logic in
# safexpress2/pod_service.py), so U_TN must be checked too, not just the
# transportation code, to avoid picking up DSR's invoices.
SAP_TRANSPORTATION_CODE_SAFEXPRESS = 1

# Transporter names (SAP UDF U_TN) known NOT to be SafeExpress, even when
# TransportationCode = 1 — never attempt these invoices via the SafeExpress
# API. DSR Logistics status is still an open item (see
# docs/architecture/PLATFORM_ARCHITECTURE.md §5) — kept excluded here
# defensively until confirmed either way.
NON_SAFEXPRESS_TRANSPORTERS: list[str] = [
    "DP World", "Delhivery", "Local delivery partner", "Platform Fulfilled",
    "DSR Logistics",
]

TRANSPORTER_NAME_SAFEXPRESS = "Safexpress"

# SAP field names (AR Invoice / Delivery Note UDFs) — real, working fields
# confirmed live in the legacy integration.
SAP_FIELD_TRANSPORTER_NAME = "U_TN"
SAP_FIELD_TRACKING_NUMBER = "U_TrackingNo"
SAP_FIELD_POD_LINK = "U_Pod_Link"
SAP_FIELD_POD_DATE = "U_POD_DATE"
# Real column verified 2026-08-28 against prod OINV (SYS.TABLE_COLUMNS) —
# "TransportationCode" (the legacy pipeline's own field name) doesn't
# exist on this schema; SAP B1's actual header field is "TrnspCode".
SAP_FIELD_TRANSPORTATION_CODE = "TrnspCode"

DOC_TYPE_INVOICE = "INV"
DOC_TYPE_WAYBILL = "WB"

DATE_FORMATS: list[str] = [
    "%d-%b-%Y",  # 25-Nov-2024
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
]

DEFAULT_LOOKBACK_DAYS = 4
MAX_LOOKBACK_DAYS = 500
DEFAULT_BATCH_SIZE = 50
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_POD_FETCH_TIMEOUT_SECONDS = 60
DEFAULT_UPLOAD_TIMEOUT_SECONDS = 60

MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
RETRY_BACKOFF_MULTIPLIER = 2.0

POD_STATUS_PENDING = "pending"
POD_STATUS_WAYBILL_FOUND = "waybill_found"
POD_STATUS_PENDING_SAP = "pending_sap"
POD_STATUS_COMPLETED = "completed"
POD_STATUS_NO_WAYBILL = "no_waybill"
POD_STATUS_FAILED = "failed"
# Terminal give-up state — an invoice that's been open (not completed) for
# longer than STALE_AFTER_DAYS is more likely cancelled or mis-tagged with
# the wrong transporter than genuinely still in transit. Stops the backlog
# growing forever; a fresh HANA sighting (find_candidates) revives it.
POD_STATUS_STALE = "stale"

# A `failed` row (an unexpected error: a POD fetch that timed out, a Safexpress
# outage) is retried like any open row until it has failed this many times, so
# one bad moment no longer parks an invoice for good. ~20 tries at the 3-hourly
# schedule is about 2.5 days, enough to outlast an outage but not to hammer a
# row that is genuinely broken; those stay `failed` and show on the dashboard.
MAX_FAILED_ATTEMPTS = 20

# Stop starting new invoices when this little Lambda time is left, so a slow
# batch ends cleanly instead of being killed mid-invoice (an upload done but
# not yet recorded). The rest of the batch is picked up by the next run.
MIN_RUN_BUDGET_SECONDS = 120

# This many invoices failing back to back is treated as an outage (see
# pod_service.poll_and_process) and ends the run early.
MAX_CONSECUTIVE_FAILURES = 5

# Rows still open (pending/waybill_found/no_waybill) past this age stop
# being auto-retried every run — see store.mark_stale_if_expired.
STALE_AFTER_DAYS = 120

# Backlog processing order: mostly newest-invoice-first (recent shipments
# are far more likely to already have a waybill/POD ready than old ones),
# with a small reserved slice for the oldest open rows each run so nothing
# waits forever behind a growing stream of new invoices.
OLDEST_FAIRNESS_FRACTION = 0.1

"""SAP document constants for the Amazon US B2C channel (INC entity).

Every value here was verified against the production company DB
KF_INC_22012026 (HANA-checked, not assumed) during the Frappe pipeline
rollout. Change via environment only for the TEST-DB validation phase.
"""

from __future__ import annotations

import os


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


SAP_CARD_CODE = _env("AMZ_SAP_CARD_CODE", "C00001")            # "Amazon USA"
SAP_INVOICE_SERIES = int(_env("AMZ_SAP_INVOICE_SERIES", "120"))  # KFIB2C26
SAP_CN_SERIES = int(_env("AMZ_SAP_CN_SERIES", "104"))            # KFIB2C26
SAP_WAREHOUSE = _env("AMZ_SAP_WAREHOUSE", "AMZ-FBA")
SAP_TAX_CODE = _env("AMZ_SAP_TAX_CODE", "etax0")  # exact lowercase; OData filters are case-sensitive
SAP_PAY_TO_CODE = _env("AMZ_SAP_PAY_TO_CODE", "Amazon USA-B2C")
SAP_CREATED_BY = _env("AMZ_SAP_CREATED_BY", "Posted By Amazon API")  # OINV/ORIN mandatory UDF

SAP_TAX_ITEM_CODE = "EXP000003"                    # "Tax-Sum"
SAP_TAX_GL_FORMAT = "22200000-01-001-01"           # Sales Tax Accrual (_SYS00000000059)
SAP_SHIPPING_ITEM_CODE = "EXP000004"               # "Shipping Charges"
SAP_SHIPPING_GL_FORMAT = "41400000-01-001-01"      # Sales Revenues - Freight (_SYS00000000084)

# Orders shipped before this date were manually booked by Finance from the
# MTR (pre-pipeline). Posting them again would double-count revenue.
POSTING_CUTOFF_DATE = _env("AMZ_POSTING_CUTOFF_DATE", "2026-06-01")

MARKETPLACE_ID = _env("AMZ_MARKETPLACE_ID", "ATVPDKIKX0DER")   # amazon.com
SP_API_BASE = _env("AMZ_SP_API_BASE", "https://sellingpartnerapi-na.amazon.com")
LWA_TOKEN_URL = _env("AMZ_LWA_TOKEN_URL", "https://api.amazon.com/auth/o2/token")

# MTR keys on shipment/settlement date while order fetch filters purchase
# date: a June MTR contains orders purchased weeks earlier. 30 days covers
# every gap observed in the June 2026 reconciliation (7 was too short).
FETCH_LOOKBACK_DAYS = int(_env("AMZ_FETCH_LOOKBACK_DAYS", "30"))

# MTR recon detail CSVs land here (recon.run_mtr_recon writes them under the
# "recon/" prefix); the dashboard's recon-download route presigns from here.
RECON_BUCKET = _env("AMZ_RECON_BUCKET")

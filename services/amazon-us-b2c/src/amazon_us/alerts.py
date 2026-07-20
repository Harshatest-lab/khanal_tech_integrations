"""SES failure/summary alerts (same delivery mechanism as the Walmart
service's sap_alert_email, generalized for this pipeline)."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


ADMIN_EMAIL = _env("AMZ_ALERT_ADMIN_EMAIL")
FROM_EMAIL = _env("AMZ_ALERT_FROM_EMAIL")
SES_REGION = _env("AMZ_SES_REGION", "ap-south-1")


def send_alert(subject: str, body: str) -> bool:
    """Best-effort admin email. Never raises — an alert failure must not
    fail the pipeline run it is reporting on."""
    if not (ADMIN_EMAIL and FROM_EMAIL):
        logger.info("Alert email not configured; skipping: %s", subject)
        return False
    try:
        import boto3

        ses = boto3.client("ses", region_name=SES_REGION)
        ses.send_email(
            Source=FROM_EMAIL,
            Destination={"ToAddresses": [addr.strip() for addr in ADMIN_EMAIL.split(",") if addr.strip()]},
            Message={
                "Subject": {"Data": f"[Amazon US B2C] {subject}"},
                "Body": {"Text": {"Data": body}},
            },
        )
        return True
    except Exception:
        logger.error("SES alert send failed for %r", subject, exc_info=True)
        return False

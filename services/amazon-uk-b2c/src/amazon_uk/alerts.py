"""SES failure alerts (same delivery mechanism as the Amazon US service).

Configured by env vars; when the admin/from addresses are unset this is a
no-op, so local runs and un-configured stages never need SES.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Secrets that could be echoed back inside a driver/HTTP exception message
# (LWA token exchange failures include the response text, psycopg connection
# errors include host/user, etc.). Scrubbed before anything goes in an email.
#
# Values are pulled from the credential loaders (env or SSM, whichever is
# active) rather than raw env vars — once a secret moves to SSM the plain
# env var is unset, and scrubbing from an empty string is a silent no-op.
_SECRET_ENV_VARS = (
    "DB_PASSWORD",
    "HANA_PASSWORD",
    "SAP_SL_PASSWORD",
    "LWA_CLIENT_SECRET",
    "LWA_REFRESH_TOKEN",
)


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _current_secrets() -> tuple[str, ...]:
    """Best-effort: never raise — alerting must survive missing/broken creds."""
    from amazon_uk import credentials

    secrets = [_env(var) for var in _SECRET_ENV_VARS]
    try:
        db = credentials.get_db_credentials()
        secrets.append(db.password)
    except Exception:
        pass
    try:
        sap = credentials.get_sap_sl_credentials()
        secrets.append(sap.password)
    except Exception:
        pass
    try:
        lwa = credentials.get_lwa_credentials()
        secrets.append(lwa.client_secret)
        secrets.append(lwa.refresh_token)
    except Exception:
        pass
    try:
        from khanal_shared.hana.config import get_hana_params

        hana = get_hana_params()
        if hana is not None:
            secrets.append(hana.password)
    except Exception:
        pass
    return tuple(secrets)


def scrub(text: str) -> str:
    """Redact any configured secret value from ``text``."""
    for secret in _current_secrets():
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return re.sub(r"://[^/@\s]+:[^/@\s]+@", "://***:***@", text)


def _fmt(value) -> str:
    return ", ".join(f"{k}={v}" for k, v in value.items()) if isinstance(value, dict) else str(value)


def build_report(label: str, result) -> tuple[str, str]:
    """(subject, body) summarising a finished scheduled job.

    Posting results ({total, posted, failed, results: [...]}) get posted /
    failed / recovered counts plus every failed item with its error; fetch
    results (orders, items_stored, shipments_stored, ...) are listed as-is.
    """
    result = result if isinstance(result, dict) else {"result": result}
    rows = result.get("results") if isinstance(result.get("results"), list) else None
    failed_n = int(result.get("failed") or 0)
    status = "completed with FAILURES" if failed_n else "completed"
    if result.get("budget_exhausted"):
        status = "stopped early (out of time, more orders remain — continuing)"
    lines = [f"Amazon UK {label} {status}", ""]
    for key, value in result.items():
        if key != "results":
            lines.append(f"{key}: {_fmt(value)}")
    if rows is not None:
        by_status: dict[str, int] = {}
        for row in rows:
            by_status[row.get("status", "?")] = by_status.get(row.get("status", "?"), 0) + 1
        lines += ["", "By status: " + (", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "none")]
        failures = [r for r in rows if r.get("status") == "failed"]
        if failures:
            lines += ["", f"Failed ({len(failures)}):"]
            lines += [f"  {r.get('order') or r.get('transaction_id')}: {r.get('error')}" for r in failures[:100]]
            if len(failures) > 100:
                lines.append(f"  ... and {len(failures) - 100} more (see dashboard / amazonuk.sap_postings)")
    return f"{label} {status}" + (f" ({failed_n} failed)" if failed_n else ""), "\n".join(lines)


DEFAULT_SENDER = "varshap@khanalfoods.com"
DEFAULT_REPORT_EMAILS = "varshap@khanalfoods.com"
DEFAULT_FAILURE_EMAILS = "yaknaprabha@khanalfoods.com,finance@khanalfoods.com"

# The only posting failures that email the SAP/finance team: SKU not mapped to
# an SAP item, and SAP posting period locked. Matched against the error text
# recorded for the order/refund. First match wins.
_FAILURE_CATEGORIES = (
    ("SAP item mapping", re.compile(r"no sap mapping", re.I)),
    ("SAP posting period locked", re.compile(r"posting period|period.{0,20}lock|lock.{0,20}period", re.I)),
)


def categorize_failure(error) -> str | None:
    text = str(error or "")
    for name, pattern in _FAILURE_CATEGORIES:
        if pattern.search(text):
            return name
    return None


def send_failure_alert(label: str, result) -> bool:
    """Email AMZ_UK_FAILURE_ALERT_EMAILS when a posting run has failures of
    the two kinds above (one email per run, grouped by category).
    No-op for fetch jobs and runs with nothing to report."""
    try:
        rows = result.get("results") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return False
        grouped: dict[str, list[str]] = {}
        for row in rows:
            if row.get("status") != "failed":
                continue
            category = categorize_failure(row.get("error"))
            if category:
                grouped.setdefault(category, []).append(
                    f"  {row.get('order') or row.get('transaction_id')}: {row.get('error')}"
                )
        if not grouped:
            return False
        total = sum(len(v) for v in grouped.values())
        lines = [f"Amazon UK {label}: {total} posting failure(s) need attention", ""]
        for category, items in grouped.items():
            lines += [f"{category} ({len(items)}):", *items[:100]]
            if len(items) > 100:
                lines.append(f"  ... and {len(items) - 100} more (see dashboard)")
            lines.append("")
        return send_alert(
            f"{label}: {total} posting failure(s) ({', '.join(grouped)})",
            scrub("\n".join(lines)),
            to=_env("AMZ_UK_FAILURE_ALERT_EMAILS", DEFAULT_FAILURE_EMAILS),
        )
    except Exception:
        logger.error("failure alert email failed for %r", label, exc_info=True)
        return False


def send_daily_report(text: str, subject: str) -> bool:
    """Best-effort daily counts email to AMZ_UK_REPORT_EMAILS."""
    return send_alert(subject, scrub(text), to=_env("AMZ_UK_REPORT_EMAILS", DEFAULT_REPORT_EMAILS))


def send_alert(subject: str, body: str, to: str = "") -> bool:
    """Best-effort email (``to`` = comma-separated; default the admin
    address). Never raises — an alert failure must not fail the pipeline
    run it is reporting on."""
    admin = to or _env("AMZ_UK_ALERT_ADMIN_EMAIL", DEFAULT_REPORT_EMAILS)
    sender = _env("AMZ_UK_ALERT_FROM_EMAIL", DEFAULT_SENDER)
    if not (admin and sender):
        logger.info("Alert email not configured; skipping: %s", subject)
        return False
    try:
        import boto3

        ses = boto3.client("ses", region_name=_env("AMZ_UK_SES_REGION", "ap-south-1"))
        ses.send_email(
            Source=sender,
            Destination={"ToAddresses": [addr.strip() for addr in admin.split(",") if addr.strip()]},
            Message={
                "Subject": {"Data": f"[Amazon UK B2C] {subject}"},
                "Body": {"Text": {"Data": body}},
            },
        )
        return True
    except Exception:
        logger.error("SES alert send failed for %r", subject, exc_info=True)
        return False

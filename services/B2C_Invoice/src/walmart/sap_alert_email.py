"""Admin email alerts for Walmart B2C SAP invoice sync failures (Amazon SES)."""

from __future__ import annotations

import html
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_DASHBOARD_URL = (
    "http://walmart-dashboard-khanalb2c.s3-website.ap-south-1.amazonaws.com/"
)

# Email-safe palette (inline styles only — no external CSS).
_COLOR_HEADER = "#0f172a"
_COLOR_ACCENT = "#2563eb"
_COLOR_DANGER = "#dc2626"
_COLOR_WARNING = "#d97706"
_COLOR_MUTED = "#64748b"
_COLOR_BORDER = "#e2e8f0"
_COLOR_SURFACE = "#f8fafc"
_COLOR_ERROR_BG = "#fef2f2"


def _admin_recipients() -> list[str]:
    raw = (os.environ.get("WALMART_SAP_ALERT_ADMIN_EMAIL") or "").strip()
    if not raw:
        return []
    return [addr.strip() for addr in raw.split(",") if addr.strip()]


def _from_email() -> str | None:
    value = (os.environ.get("WALMART_SAP_ALERT_FROM_EMAIL") or "").strip()
    return value or None


def _dashboard_url() -> str:
    return (
        os.environ.get("WALMART_DASHBOARD_URL") or DEFAULT_DASHBOARD_URL
    ).rstrip("/") + "/"


def sap_alert_email_enabled() -> bool:
    return bool(_admin_recipients() and _from_email())


def _esc(value: str | None) -> str:
    return html.escape((value or "").strip() or "—", quote=True)


def _phase_label(phase: str) -> str:
    labels = {
        "build": "Build (payload / HANA lookup)",
        "post": "Post (SAP Service Layer)",
        "max_retries_exceeded": "Max retries exceeded",
    }
    key = (phase or "").strip().lower()
    return labels.get(key, phase or "—")


def build_sap_failure_email(
    *,
    purchase_order_id: str,
    customer_order_id: str,
    error_phase: str,
    error_message: str,
    attempt_count: int | None = None,
    max_retries_exceeded: bool = False,
) -> tuple[str, str, str]:
    """Return (subject, plain_text_body, html_body) for a SAP failure alert."""
    attempt_label = str(attempt_count) if attempt_count is not None else "—"
    phase_display = _phase_label(error_phase)
    error_text = (error_message or "").strip() or "(no message)"
    dashboard = _dashboard_url()

    if max_retries_exceeded:
        subject = (
            f"[Walmart B2C] SAP max retries reached — PO {purchase_order_id}"
        )
        headline = "Max retries reached"
        badge_color = _COLOR_WARNING
        badge_text = "Stopped"
    else:
        subject = (
            f"[Walmart B2C] SAP post failed — PO {purchase_order_id} "
            f"(attempt {attempt_label})"
        )
        headline = "SAP invoice sync failed"
        badge_color = _COLOR_DANGER
        badge_text = "Failed"

    lines = [
        "Walmart B2C — SAP invoice sync failure",
        "",
        f"PO number:        {purchase_order_id}",
        f"Customer order:   {customer_order_id or '—'}",
        f"Phase:            {phase_display}",
        f"Sync attempt:     {attempt_label}",
    ]
    if max_retries_exceeded:
        lines.append("Status:           max retries exceeded (EventBridge will not retry)")
    lines.extend(
        [
            "",
            "Error:",
            error_text,
            "",
            f"Dashboard: {dashboard}",
            "",
            "Open the dashboard → filter SAP sync = Failed, or search by PO number.",
        ]
    )
    plain_body = "\n".join(lines)

    html_body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background-color:#eef2f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1e293b;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:#eef2f7;padding:24px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:12px;overflow:hidden;border:1px solid {_COLOR_BORDER};">
          <tr>
            <td style="background-color:{_COLOR_HEADER};padding:28px 32px 24px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td>
                    <p style="margin:0 0 6px;font-size:12px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#94a3b8;">Walmart B2C</p>
                    <h1 style="margin:0;font-size:22px;line-height:1.3;font-weight:700;color:#ffffff;">{_esc(headline)}</h1>
                  </td>
                  <td align="right" valign="top">
                    <span style="display:inline-block;padding:6px 12px;border-radius:999px;background-color:{badge_color};color:#ffffff;font-size:12px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;">{_esc(badge_text)}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 32px 8px;">
              <p style="margin:0 0 18px;font-size:15px;line-height:1.6;color:#334155;">
                An order could not be posted to SAP. Review the details below and retry from the dashboard if needed.
              </p>
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background-color:{_COLOR_SURFACE};border:1px solid {_COLOR_BORDER};border-radius:10px;">
                <tr>
                  <td style="padding:16px 18px;border-bottom:1px solid {_COLOR_BORDER};">
                    <p style="margin:0 0 4px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:{_COLOR_MUTED};">PO number</p>
                    <p style="margin:0;font-size:16px;font-weight:700;color:#0f172a;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;">{_esc(purchase_order_id)}</p>
                  </td>
                </tr>
                <tr>
                  <td style="padding:16px 18px;border-bottom:1px solid {_COLOR_BORDER};">
                    <p style="margin:0 0 4px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:{_COLOR_MUTED};">Customer order</p>
                    <p style="margin:0;font-size:15px;font-weight:600;color:#0f172a;">{_esc(customer_order_id)}</p>
                  </td>
                </tr>
                <tr>
                  <td style="padding:16px 18px;border-bottom:1px solid {_COLOR_BORDER};">
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                      <tr>
                        <td width="50%" valign="top" style="padding-right:8px;">
                          <p style="margin:0 0 4px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:{_COLOR_MUTED};">Phase</p>
                          <p style="margin:0;font-size:14px;font-weight:600;color:#0f172a;">{_esc(phase_display)}</p>
                        </td>
                        <td width="50%" valign="top" style="padding-left:8px;">
                          <p style="margin:0 0 4px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:{_COLOR_MUTED};">Sync attempt</p>
                          <p style="margin:0;font-size:14px;font-weight:600;color:#0f172a;">{_esc(attempt_label)}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
                {"<tr><td style=\"padding:14px 18px;background-color:#fffbeb;border-bottom:1px solid #fde68a;\"><p style=\"margin:0;font-size:13px;line-height:1.5;color:#92400e;\"><strong>EventBridge will not retry</strong> this order automatically. Use <strong>Post-SAP</strong> on the dashboard after fixing the issue.</p></td></tr>" if max_retries_exceeded else ""}
                <tr>
                  <td style="padding:16px 18px;">
                    <p style="margin:0 0 8px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:{_COLOR_MUTED};">Error</p>
                    <div style="background-color:{_COLOR_ERROR_BG};border:1px solid #fecaca;border-radius:8px;padding:14px 16px;">
                      <pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.55;color:#991b1b;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;">{_esc(error_text)}</pre>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px 28px;" align="center">
              <a href="{_esc(dashboard)}" style="display:inline-block;background-color:{_COLOR_ACCENT};color:#ffffff;text-decoration:none;font-size:14px;font-weight:700;padding:14px 28px;border-radius:8px;">Open Walmart dashboard</a>
              <p style="margin:16px 0 0;font-size:13px;line-height:1.5;color:{_COLOR_MUTED};">
                Filter <strong>SAP sync = Failed</strong> or search by PO number.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px;background-color:{_COLOR_SURFACE};border-top:1px solid {_COLOR_BORDER};">
              <p style="margin:0;font-size:12px;line-height:1.5;color:{_COLOR_MUTED};text-align:center;">
                Khanal Tech · Walmart B2C invoice sync · Automated alert
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    return subject, plain_body, html_body


def send_sap_failure_alert(
    *,
    purchase_order_id: str,
    customer_order_id: str,
    error_phase: str,
    error_message: str,
    attempt_count: int | None = None,
    max_retries_exceeded: bool = False,
) -> dict[str, Any]:
    """
    Send admin alert via SES. Never raises — returns {sent, skipped, error?}.
    Configure WALMART_SAP_ALERT_ADMIN_EMAIL and WALMART_SAP_ALERT_FROM_EMAIL.
    """
    recipients = _admin_recipients()
    from_email = _from_email()
    if not recipients or not from_email:
        logger.info(
            "SAP failure alert skipped (email not configured): PO %s phase=%s",
            purchase_order_id,
            error_phase,
        )
        return {"sent": False, "skipped": True, "reason": "email_not_configured"}

    subject, text_body, html_body = build_sap_failure_email(
        purchase_order_id=purchase_order_id,
        customer_order_id=customer_order_id,
        error_phase=error_phase,
        error_message=error_message,
        attempt_count=attempt_count,
        max_retries_exceeded=max_retries_exceeded,
    )

    region = (
        os.environ.get("WALMART_SES_REGION")
        or os.environ.get("AWS_REGION")
        or "ap-south-1"
    )
    try:
        import boto3

        client = boto3.client("ses", region_name=region)
        client.send_email(
            Source=from_email,
            Destination={"ToAddresses": recipients},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": text_body, "Charset": "UTF-8"},
                    "Html": {"Data": html_body, "Charset": "UTF-8"},
                },
            },
        )
        logger.info(
            "SAP failure alert sent for PO %s to %s",
            purchase_order_id,
            recipients,
        )
        return {"sent": True, "recipients": recipients}
    except Exception as exc:
        logger.exception(
            "SAP failure alert email failed for PO %s: %s",
            purchase_order_id,
            exc,
        )
        return {"sent": False, "error": str(exc)}

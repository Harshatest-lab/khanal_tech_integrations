"""Tests for SAP failure admin email alerts."""

from walmart.sap_alert_email import build_sap_failure_email, sap_alert_email_enabled


def test_build_sap_failure_email_includes_dashboard_and_error(monkeypatch):
    monkeypatch.setenv(
        "WALMART_DASHBOARD_URL",
        "http://walmart-dashboard-khanalb2c.s3-website.ap-south-1.amazonaws.com/",
    )
    subject, text_body, html_body = build_sap_failure_email(
        purchase_order_id="119118392688196",
        customer_order_id="200015030709332",
        error_phase="build",
        error_message='relation "sap_fg_items" does not exist',
        attempt_count=2,
    )
    assert "119118392688196" in subject
    assert "attempt 2" in subject
    assert "sap_fg_items" in text_body
    assert "walmart-dashboard-khanalb2c.s3-website.ap-south-1.amazonaws.com" in text_body
    assert "Build (payload / HANA lookup)" in text_body
    assert "119118392688196" in html_body
    assert "Open Walmart dashboard" in html_body
    assert "sap_fg_items" in html_body


def test_build_sap_failure_email_max_retries_subject(monkeypatch):
    subject, text_body, html_body = build_sap_failure_email(
        purchase_order_id="PO1",
        customer_order_id="CO1",
        error_phase="max_retries_exceeded",
        error_message="stopped",
        attempt_count=3,
        max_retries_exceeded=True,
    )
    assert "max retries reached" in subject
    assert "max retries exceeded" in text_body
    assert "EventBridge will not retry" in html_body


def test_sap_alert_email_enabled_requires_from_and_admin(monkeypatch):
    monkeypatch.delenv("WALMART_SAP_ALERT_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("WALMART_SAP_ALERT_FROM_EMAIL", raising=False)
    assert sap_alert_email_enabled() is False

    monkeypatch.setenv("WALMART_SAP_ALERT_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("WALMART_SAP_ALERT_FROM_EMAIL", "alerts@example.com")
    assert sap_alert_email_enabled() is True

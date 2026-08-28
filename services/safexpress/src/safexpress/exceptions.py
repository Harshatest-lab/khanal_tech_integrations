"""Exceptions for the SafeExpress integration.

Ported from khanal_tech_integrations/utils/safexpress2/exceptions.py — same
hierarchy, same is_retryable() split between transient errors (worth a
retry) and business-logic outcomes like "no waybill for this invoice yet"
(not an error, just means this invoice hasn't shipped via SafeExpress).
"""

from __future__ import annotations


class SafexpressError(Exception):
    """Base exception for all SafeExpress-related errors."""


class SafexpressAuthenticationError(SafexpressError):
    """Authentication with the SafeExpress API failed."""


class SafexpressAPIError(SafexpressError):
    def __init__(self, message: str, status_code: int | None = None, response_text: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

    def is_retryable(self) -> bool:
        if "No waybill found" in (self.args[0] if self.args else ""):
            return False
        if self.status_code == 504:
            return True
        if self.status_code and self.status_code >= 500:
            return True
        if self.status_code and 400 <= self.status_code < 500:
            return False
        return True


class SafexpressWaybillNotFoundError(SafexpressAPIError):
    """No waybill yet for this invoice — business-as-usual, not retryable."""

    def __init__(self, message: str, invoice_number: str | None = None) -> None:
        super().__init__(message, status_code=404)
        self.invoice_number = invoice_number

    def is_retryable(self) -> bool:
        return False


class SafexpressPODNotFoundError(SafexpressError):
    """POD document not yet available for a waybill."""


class SafexpressSAPIntegrationError(SafexpressError):
    """SAP B1 Service Layer PATCH failed."""


class SafexpressFileUploadError(SafexpressError):
    """POD upload to S3 failed."""

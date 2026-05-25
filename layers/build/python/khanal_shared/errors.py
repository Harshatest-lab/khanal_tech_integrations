"""Application-level errors (map to HTTP in handlers)."""

from __future__ import annotations


class AppError(Exception):
    """Base class for expected failures."""

    http_status = 500
    error_code = "internal_error"


class ValidationError(AppError):
    http_status = 400
    error_code = "validation_error"


class NotFoundError(AppError):
    http_status = 404
    error_code = "not_found"


class ConflictError(AppError):
    http_status = 409
    error_code = "conflict"


class UpstreamError(AppError):
    http_status = 502
    error_code = "upstream_error"

"""Database package — connection helpers for RDS Proxy + PostgreSQL."""

from khanal_shared.db.pool import get_pool

__all__ = ["get_pool"]

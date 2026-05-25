"""Load configuration from environment variables (and optionally SSM at runtime)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    stage: str
    database_url: str | None
    log_level: str
    frappe_ingest_api_key: str | None
    delhivery_webhook_secret: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            stage=os.getenv("STAGE", "dev"),
            database_url=os.getenv("DATABASE_URL"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            frappe_ingest_api_key=os.getenv("FRAPPE_INGEST_API_KEY"),
            delhivery_webhook_secret=os.getenv("DELHIVERY_WEBHOOK_SECRET"),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()

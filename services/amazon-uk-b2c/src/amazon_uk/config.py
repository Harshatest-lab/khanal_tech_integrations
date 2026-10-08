"""Pipeline configuration for the Lambda deployment — from environment variables.

Same ``get(key, default)`` API the standalone pipeline's modules import, but
backed by ``os.environ`` (set via ``serverless.yml`` ``provider.environment``
at deploy time) instead of a local ``.env`` file.

If a variable is missing, the pipeline fails loudly instead of guessing.
"""

from __future__ import annotations

import os


def get(key: str, default: str | None = None) -> str:
    """Return an environment variable; raises if missing and no default."""
    value = (os.environ.get(key) or "").strip()
    if value:
        return value
    if default is not None:
        return default
    raise RuntimeError(f"Environment variable '{key}' is not set")

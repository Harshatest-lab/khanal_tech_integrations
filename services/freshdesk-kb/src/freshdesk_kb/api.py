"""GET /v1/freshdesk/kb?action=index | article&id=<id> | search&q=<text>.

Public and read-only: it only ever returns published articles from folders
visible to all users, so no auth — CORS and API Gateway throttling bound it.
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from khanal_shared.logging import log_json

from . import credentials, kb

logger = logging.getLogger(__name__)

ACTIONS = frozenset({"index", "article", "search"})
MAX_QUERY_LENGTH = 120


def _json(status: int, body: dict[str, Any], max_age: int = 0) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": f"public, max-age={max_age}" if max_age else "no-store",
        },
        "body": json.dumps(body),
    }


def handle(event: dict[str, Any]) -> dict[str, Any]:
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "GET")
    if method != "GET":
        return _json(405, {"error": "Method not allowed"})

    params = event.get("queryStringParameters") or {}
    action = params.get("action")
    if action not in ACTIONS:
        return _json(400, {"error": "Unknown action"})

    try:
        data = kb.get(credentials.get_config())
    except credentials.FreshdeskConfigError as e:
        log_json(logger, logging.ERROR, "freshdesk_kb_not_configured", error=str(e))
        return _json(500, {"error": "Not configured"})
    except Exception:
        log_json(logger, logging.ERROR, "freshdesk_kb_unavailable", trace=traceback.format_exc())
        return _json(502, {"error": "Knowledge base unavailable"})

    if action == "index":
        return _json(200, {"folders": data.folders}, max_age=300)

    if action == "article":
        raw_id = str(params.get("id") or "")
        article = data.articles.get(int(raw_id)) if raw_id.isdigit() else None
        if article is None:
            return _json(404, {"error": "Not found"})
        return _json(200, {"id": article.id, "title": article.title, "html": article.html}, max_age=300)

    query = str(params.get("q") or "").strip()[:MAX_QUERY_LENGTH]
    results = kb.search(data, query) if len(query) >= 2 else []
    return _json(200, {"results": results}, max_age=60)

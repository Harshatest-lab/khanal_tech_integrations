"""Knowledge base snapshot: published articles from public folders, cached per container.

The whole knowledge base is small (tens of articles), so it is loaded in one go
and searched in memory. That keeps Freshdesk API usage to 1 + (number of folders)
calls per container per cache period, however much storefront traffic there is.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from khanal_shared.logging import log_json

from . import client
from .credentials import FreshdeskConfig

logger = logging.getLogger(__name__)

ARTICLE_PUBLISHED = 2
FOLDER_VISIBLE_TO_ALL = 1
CACHE_SECONDS = int(os.environ.get("FRESHDESK_KB_CACHE_MINUTES") or 60) * 60
RETRY_AFTER_FAILURE_SECONDS = 60
SEARCH_LIMIT = 5

STOP_WORDS = frozenset(
    {
        "the", "and", "for", "are", "you", "your", "can", "does", "with", "what", "how",
        "when", "where", "why", "this", "that", "have", "will", "from", "about", "there",
        "any", "get",
    }
)
_WORD_SPLIT = re.compile(r"[^a-z0-9\u0900-\u097f]+")


@dataclass(frozen=True)
class Article:
    id: int
    folder_id: int
    title: str
    html: str
    title_lc: str
    text_lc: str


@dataclass(frozen=True)
class KnowledgeBase:
    folders: list[dict[str, Any]]
    articles: dict[int, Article]


def fetch(config: FreshdeskConfig) -> KnowledgeBase:
    with ThreadPoolExecutor(max_workers=8) as pool:
        folder_lists = list(pool.map(lambda cid: client.list_folders(config, cid), config.category_ids))
        folders = [
            f for folders in folder_lists for f in folders if f.get("visibility") == FOLDER_VISIBLE_TO_ALL
        ]
        article_lists = list(pool.map(lambda f: client.list_articles(config, f["id"]), folders))

    articles: dict[int, Article] = {}
    index: list[dict[str, Any]] = []
    for folder, folder_articles in zip(folders, article_lists):
        published = [a for a in folder_articles if a.get("status") == ARTICLE_PUBLISHED]
        if not published:
            continue
        for a in published:
            title = a.get("title") or ""
            articles[a["id"]] = Article(
                id=a["id"],
                folder_id=folder["id"],
                title=title,
                html=a.get("description") or "",
                title_lc=title.lower(),
                text_lc=(a.get("description_text") or "").lower(),
            )
        index.append(
            {
                "id": folder["id"],
                "name": folder.get("name") or "",
                "articles": [{"id": a["id"], "title": a.get("title") or ""} for a in published],
            }
        )
    return KnowledgeBase(folders=index, articles=articles)


_cache: KnowledgeBase | None = None
_cached_at = 0.0


def get(config: FreshdeskConfig, now: Callable[[], float] = time.monotonic) -> KnowledgeBase:
    """Cached snapshot; on refresh failure keeps serving the last good one."""
    global _cache, _cached_at
    if _cache is not None and now() - _cached_at < CACHE_SECONDS:
        return _cache
    try:
        fresh = fetch(config)
    except Exception as e:
        if _cache is None:
            raise
        log_json(logger, logging.WARNING, "freshdesk_kb_refresh_failed_serving_stale", error=str(e))
        _cached_at = now() - CACHE_SECONDS + RETRY_AFTER_FAILURE_SECONDS
        return _cache
    _cache, _cached_at = fresh, now()
    return fresh


def reset_cache() -> None:
    global _cache, _cached_at
    _cache, _cached_at = None, 0.0


def search(kb: KnowledgeBase, query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    words = [w for w in _WORD_SPLIT.split(query.lower()) if len(w) > 2 and w not in STOP_WORDS]
    if not words:
        return []
    scored = []
    for a in kb.articles.values():
        score = sum((3 if w in a.title_lc else 0) + (1 if w in a.text_lc else 0) for w in words)
        if score:
            scored.append((score, a))
    scored.sort(key=lambda s: -s[0])
    return [{"id": a.id, "title": a.title} for _, a in scored[:limit]]

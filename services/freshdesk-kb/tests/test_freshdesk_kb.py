import json

import pytest

from freshdesk_kb import api, client, credentials, kb

CONFIG = credentials.FreshdeskConfig(domain="example.freshdesk.com", api_key="k", category_ids=(10,))

FRESHDESK = {
    "/solutions/categories/10/folders": [
        {"id": 1, "name": "Orders & Delivery", "visibility": 1},
        {"id": 2, "name": "Internal", "visibility": 3},
    ],
    "/solutions/folders/1/articles?per_page=100": [
        {
            "id": 101,
            "title": "How do I track my order?",
            "status": 2,
            "description": "<p>Use the tracking link</p>",
            "description_text": "Use the tracking link in your shipping email",
        },
        {"id": 102, "title": "Draft refund policy", "status": 1, "description_text": "refund"},
        {
            "id": 103,
            "title": "How long does delivery take?",
            "status": 2,
            "description": "<p>3-5 days</p>",
            "description_text": "Delivery takes 3-5 business days",
        },
    ],
}


@pytest.fixture
def freshdesk_calls(monkeypatch):
    calls = []

    def fake_get(config, path):
        calls.append(path)
        if path not in FRESHDESK:
            raise AssertionError(f"unexpected Freshdesk call {path}")
        return FRESHDESK[path]

    kb.reset_cache()
    monkeypatch.setattr(client, "get", fake_get)
    monkeypatch.setattr(credentials, "get_config", lambda: CONFIG)
    yield calls
    kb.reset_cache()


def call(params, method="GET"):
    resp = api.handle({"queryStringParameters": params, "requestContext": {"http": {"method": method}}})
    return resp["statusCode"], json.loads(resp["body"])


def test_index_lists_only_published_articles_in_public_folders(freshdesk_calls):
    status, body = call({"action": "index"})
    assert status == 200
    assert body == {
        "folders": [
            {
                "id": 1,
                "name": "Orders & Delivery",
                "articles": [
                    {"id": 101, "title": "How do I track my order?"},
                    {"id": 103, "title": "How long does delivery take?"},
                ],
            }
        ]
    }
    assert "/solutions/folders/2/articles?per_page=100" not in freshdesk_calls


def test_article_returns_html_and_hides_drafts(freshdesk_calls):
    assert call({"action": "article", "id": "101"}) == (
        200,
        {"id": 101, "title": "How do I track my order?", "html": "<p>Use the tracking link</p>"},
    )
    assert call({"action": "article", "id": "102"})[0] == 404
    assert call({"action": "article", "id": "abc"})[0] == 404


def test_search_ranks_title_matches_and_skips_drafts(freshdesk_calls):
    assert call({"action": "search", "q": "how long is delivery"}) == (
        200,
        {"results": [{"id": 103, "title": "How long does delivery take?"}]},
    )
    assert call({"action": "search", "q": "refund"}) == (200, {"results": []})
    assert call({"action": "search", "q": "a"}) == (200, {"results": []})


def test_knowledge_base_is_cached_between_requests(freshdesk_calls):
    call({"action": "index"})
    call({"action": "search", "q": "delivery"})
    call({"action": "article", "id": "103"})
    assert len(freshdesk_calls) == 2


def test_serves_stale_copy_when_refresh_fails(freshdesk_calls, monkeypatch):
    clock = {"now": 0.0}
    kb.get(CONFIG, now=lambda: clock["now"])
    assert len(freshdesk_calls) == 2

    def failing_get(config, path):
        freshdesk_calls.append(path)
        raise client.FreshdeskApiError("Freshdesk 503")

    monkeypatch.setattr(client, "get", failing_get)
    clock["now"] = kb.CACHE_SECONDS + 1
    stale = kb.get(CONFIG, now=lambda: clock["now"])
    assert 101 in stale.articles
    assert len(freshdesk_calls) == 3

    kb.get(CONFIG, now=lambda: clock["now"])
    assert len(freshdesk_calls) == 3, "waits before retrying a failed refresh"


def test_rejects_unknown_action_and_non_get(freshdesk_calls):
    assert call({"action": "tickets"})[0] == 400
    assert call({"action": "index"}, method="POST")[0] == 405
    assert freshdesk_calls == []


def test_missing_config_returns_not_configured(monkeypatch):
    kb.reset_cache()

    def missing():
        raise credentials.FreshdeskConfigError("SSM /freshdesk/credentials not found")

    monkeypatch.setattr(credentials, "get_config", missing)
    assert call({"action": "index"}) == (500, {"error": "Not configured"})


def test_parse_config_normalises_domain_and_category_ids():
    config = credentials.parse_config(
        {"domain": "https://khanalfoods.freshdesk.com/", "api_key": " key ", "category_ids": "1, 2"}
    )
    assert config == credentials.FreshdeskConfig("khanalfoods.freshdesk.com", "key", (1, 2))

    with pytest.raises(credentials.FreshdeskConfigError, match="api_key, category_ids"):
        credentials.parse_config({"domain": "x.freshdesk.com"})

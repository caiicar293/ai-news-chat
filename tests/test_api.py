"""API behaviour, with a fake Chatter and no network access."""

from __future__ import annotations

import app as app_module
from fakes import FakeChatter


def create_chat(client, **payload):
    response = client.post("/api/chats", json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_new_chat_needs_no_topic(client):
    chat = create_chat(client)

    assert chat["query"] == ""
    assert chat["article_count"] == 0
    assert chat["message_count"] == 0
    assert chat["title"] == "New chat"
    # A new chat must not hit NewsAPI: the Chatter is built lazily.
    assert FakeChatter.fetch_calls == []


def test_chatting_works_without_headlines(client):
    chat = create_chat(client)
    response = client.post(
        f"/api/chats/{chat['id']}/messages", json={"question": "Hello there"}
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["answer"] == FakeChatter.reply
    assert body["tools"] == []
    assert [m["role"] for m in body["messages"]] == ["user", "model"]

    instruction = FakeChatter.calls[-1]["system_instruction"]
    assert "No headlines are loaded" in instruction
    assert "Get news" in instruction


def test_system_instruction_lists_loaded_headlines(client):
    chat = create_chat(client)
    fetched = client.post(
        f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 3}
    )
    assert fetched.status_code == 200, fetched.get_json()

    client.post(f"/api/chats/{chat['id']}/messages", json={"question": "Top story?"})

    instruction = FakeChatter.calls[-1]["system_instruction"]
    assert "3 headlines for 'Apple' are loaded" in instruction
    assert "list_articles_tool" in instruction


def test_news_requires_a_topic_and_a_real_chat(client):
    chat = create_chat(client)

    empty = client.post(f"/api/chats/{chat['id']}/news", json={"query": "   "})
    assert empty.status_code == 400
    assert empty.get_json()["code"] == "empty"

    missing = client.post("/api/chats/nope/news", json={"query": "Apple"})
    assert missing.status_code == 404
    assert missing.get_json()["code"] == "not_found"


def test_news_loads_articles_into_the_chat(client):
    chat = create_chat(client)
    response = client.post(
        f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 3}
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["chat"]["query"] == "Apple"
    assert body["chat"]["article_count"] == 3
    assert len(body["articles"]) == 3
    assert body["articles"][0]["index"] == 0
    assert FakeChatter.fetch_calls == [("Apple", 3)]


def test_news_clamps_page_size(client):
    chat = create_chat(client)
    client.post(
        f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 500}
    )
    session = app_module.SESSIONS[chat["id"]]
    assert session.page_size == 100

    client.post(
        f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": "abc"}
    )
    assert session.page_size == 20


def test_read_article_and_out_of_range(client):
    chat = create_chat(client)
    client.post(f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 3})

    first = client.get(f"/api/chats/{chat['id']}/articles/0")
    assert first.status_code == 200
    body = first.get_json()
    assert body["text_source"] == "full_scrape"
    assert body["text"].startswith("Full body text 1")
    assert body["characters"] > 0

    out_of_range = client.get(f"/api/chats/{chat['id']}/articles/99")
    assert out_of_range.status_code == 404
    assert out_of_range.get_json()["code"] == "not_found"


def test_message_validation(client):
    chat = create_chat(client)
    url = f"/api/chats/{chat['id']}/messages"

    empty = client.post(url, json={"question": "  "})
    assert empty.status_code == 400
    assert empty.get_json()["code"] == "empty"

    too_long = client.post(url, json={"question": "x" * 8001})
    assert too_long.status_code == 400
    assert too_long.get_json()["code"] == "too_long"

    bad_model = client.post(url, json={"question": "Hi", "model": "not a model!"})
    assert bad_model.status_code == 400
    assert bad_model.get_json()["code"] == "bad_model"


def test_model_switch_is_used_and_remembered(client):
    chat = create_chat(client)

    switched = client.patch(
        f"/api/chats/{chat['id']}", json={"model": "gemini-2.5-pro"}
    )
    assert switched.status_code == 200
    assert switched.get_json()["chat"]["model"] == "gemini-2.5-pro"

    client.post(f"/api/chats/{chat['id']}/messages", json={"question": "Hi"})
    assert FakeChatter.calls[-1]["model"] == "gemini-2.5-pro"


def test_invalid_model_is_rejected(client):
    chat = create_chat(client)
    response = client.patch(f"/api/chats/{chat['id']}", json={"model": "bad model"})

    assert response.status_code == 400
    assert response.get_json()["code"] == "bad_model"


def test_two_quick_questions_get_429_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(app_module, "MIN_SECONDS_BETWEEN_QUESTIONS", 30.0)
    chat = create_chat(client)
    url = f"/api/chats/{chat['id']}/messages"

    first = client.post(url, json={"question": "One"})
    assert first.status_code == 200
    second = client.post(url, json={"question": "Two"})

    assert second.status_code == 429
    body = second.get_json()
    assert body["code"] == "rate_limited"
    assert body["retry_after"] >= 1
    assert second.headers["Retry-After"] == str(body["retry_after"])


def test_busy_chat_returns_429(client):
    chat = create_chat(client)
    session = app_module.SESSIONS[chat["id"]]
    assert session.lock.acquire(blocking=False)
    try:
        response = client.post(
            f"/api/chats/{chat['id']}/messages", json={"question": "Hi"}
        )
    finally:
        session.lock.release()

    assert response.status_code == 429
    assert response.get_json()["code"] == "rate_limited"


def test_provider_errors_are_translated(client):
    chat = create_chat(client)
    url = f"/api/chats/{chat['id']}/messages"

    FakeChatter.fail_with = RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
    limited = client.post(url, json={"question": "Hi"})
    assert limited.status_code == 429
    assert limited.get_json()["code"] == "rate_limited"
    assert limited.get_json()["retry_after"] == 20

    FakeChatter.fail_with = RuntimeError("boom")
    broken = client.post(url, json={"question": "Hi"})
    assert broken.status_code == 502
    assert broken.get_json()["code"] == "server_error"

    # A failed turn must not be written to the transcript.
    detail = client.get(f"/api/chats/{chat['id']}").get_json()
    assert detail["message_count"] == 0


def test_describe_error_mapping():
    def code_for(message, status=None):
        error = RuntimeError(message)
        if status is not None:
            error.status_code = status
        return app_module.describe_error(error)[0]["code"], app_module.describe_error(error)[1]

    assert code_for("API key not valid") == ("auth", 502)
    assert code_for("nope", status=403) == ("auth", 502)
    assert code_for("request timed out") == ("timeout", 504)
    assert code_for("getaddrinfo failed") == ("network", 502)
    assert code_for("sleepy", status=429) == ("rate_limited", 429)


def test_unknown_chat_routes_are_404_json(client):
    for method, url, payload in (
        ("get", "/api/chats/missing", None),
        ("patch", "/api/chats/missing", {"model": "gemini-2.5-pro"}),
        ("post", "/api/chats/missing/messages", {"question": "Hi"}),
        ("delete", "/api/chats/missing", None),
        ("get", "/api/chats/missing/articles", None),
    ):
        response = getattr(client, method)(url, json=payload)

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"

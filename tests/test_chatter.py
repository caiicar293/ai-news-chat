"""Offline tests for `file.py` (no NewsAPI, no Gemini, no network)."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from google.genai import types

import file as file_module


class FakeResponse:
    def __init__(self, payload=None, text="<html></html>", status=200):
        self._payload = payload or {}
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise file_module.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeModels:
    def __init__(self):
        self.calls = []
        self.response = SimpleNamespace(
            text="Answer text.", automatic_function_calling_history=[]
        )

    def generate_content(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self.response


@pytest.fixture()
def make_chatter(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini")
    monkeypatch.setenv("NEWSAPI_KEY", "test-news")
    models = FakeModels()
    monkeypatch.setattr(
        file_module.genai, "Client", lambda **kwargs: SimpleNamespace(models=models)
    )
    monkeypatch.setattr(file_module.requests, "get", lambda *args, **kwargs: FakeResponse())
    monkeypatch.setattr(file_module.trafilatura, "extract", lambda *args, **kwargs: "Body.")

    def factory(query: str = "", **kwargs):
        kwargs.setdefault("log_tools", False)
        return file_module.Chatter(query=query, **kwargs)

    factory.models = models
    return factory


def test_empty_query_starts_without_a_newsapi_call(make_chatter, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        file_module.requests, "get", lambda *args, **kwargs: calls.append(args) or FakeResponse()
    )

    chat = make_chatter(query="")

    assert calls == []
    assert chat.articles == []
    assert chat.data == {"status": "ok", "articles": []}


def test_fetch_articles_updates_in_place_and_clamps(make_chatter, monkeypatch):
    payload = {"status": "ok", "articles": [{"title": "A"}]}
    seen: dict = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params, timeout=timeout)
        return FakeResponse(payload=payload)

    monkeypatch.setattr(file_module.requests, "get", fake_get)
    chat = make_chatter()
    articles = chat.articles

    result = chat.fetch_articles("Apple", 250)

    assert result is articles  # identity is what the article tools capture
    assert articles == [{"title": "A"}]
    assert chat.query == "Apple"
    assert seen["url"].endswith("/v2/top-headlines")
    assert seen["params"]["q"] == "Apple"
    assert seen["params"]["pageSize"] == 100
    assert seen["params"]["apiKey"] == "test-news"


def test_fetch_articles_validates_topic_and_payload(make_chatter, monkeypatch):
    chat = make_chatter()

    with pytest.raises(ValueError):
        chat.fetch_articles("   ")

    monkeypatch.setattr(
        file_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(payload={"status": "error", "message": "bad key"}),
    )
    with pytest.raises(RuntimeError, match="bad key"):
        chat.fetch_articles("Apple")


def test_list_articles_tool_omits_full_text(make_chatter):
    articles = [
        {
            "title": "T",
            "source": {"name": "S"},
            "publishedAt": "2026-11-23T09:00:00Z",
            "description": "D",
            "full_text": "body",
        }
    ]
    log: list = []
    list_tool, _ = file_module._build_article_tools(
        articles,
        lambda article: article.get("full_text", ""),
        lambda name, detail: log.append((name, detail)),
    )

    items = list_tool()

    assert items == [
        {
            "index": 0,
            "title": "T",
            "source": "S",
            "publishedAt": "2026-11-23T09:00:00Z",
            "description": "D",
            "has_cached_full_text": True,
        }
    ]
    assert "full_text" not in items[0]
    assert log == [("list_articles_tool", "count=1")]


def test_full_text_tool_bounds_and_truncation(make_chatter):
    articles = [{"title": "T", "url": "https://example.test/a", "full_text": "abcdefghij"}]
    log: list = []
    _, full_tool = file_module._build_article_tools(
        articles,
        lambda article: article.get("full_text", ""),
        lambda name, detail: log.append((name, detail)),
    )

    out_of_range = full_tool(5)
    assert "out of range" in out_of_range["error"]

    truncated = full_tool(0, max_chars=4)
    assert truncated["text"] == "abcd"
    assert truncated["truncated"] is True
    assert log[-1][0] == "fetch_article_full_text_tool"


def test_get_full_article_caches_then_refreshes(make_chatter, monkeypatch):
    requests_made: list = []

    def fake_get(url, headers=None, timeout=None):
        requests_made.append(url)
        return FakeResponse(text="<html>page</html>")

    monkeypatch.setattr(file_module.requests, "get", fake_get)
    monkeypatch.setattr(file_module.trafilatura, "extract", lambda *args, **kwargs: "Body one.")

    chat = make_chatter()
    article = {"title": "T", "url": "https://example.test/a", "description": "Desc"}

    assert chat.get_full_article(article) == "Body one."
    assert article["text_source"] == "full_scrape"
    assert chat.get_full_article(article) == "Body one."
    assert len(requests_made) == 1  # second read came from the cache

    monkeypatch.setattr(file_module.trafilatura, "extract", lambda *args, **kwargs: "Body two.")
    assert chat.get_full_article(article, force_refresh=True) == "Body two."
    assert len(requests_made) == 2


def test_get_full_article_falls_back_to_the_snippet(make_chatter, monkeypatch):
    monkeypatch.setattr(file_module.trafilatura, "extract", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        file_module.requests, "get", lambda *args, **kwargs: FakeResponse(text="<html/>")
    )
    chat = make_chatter()
    article = {"title": "T", "url": "https://example.test/a", "description": "Snippet text"}

    assert chat.get_full_article(article) == "Snippet text"
    assert article["text_source"] == "newsapi_fallback"

    def boom(*args, **kwargs):
        raise file_module.requests.RequestException("offline")

    monkeypatch.setattr(file_module.requests, "get", boom)
    unreachable = {"title": "U", "url": "https://example.test/b", "content": "API content"}
    assert chat.get_full_article(unreachable) == "API content"
    assert unreachable["text_source"] == "newsapi_fallback"


def test_save_and_load_history_round_trip(make_chatter, tmp_path):
    chat = make_chatter(history_dir=str(tmp_path))
    chat.history = [
        types.Content(role="user", parts=[types.Part.from_text(text="Hi")]),
        types.Content(role="model", parts=[types.Part.from_text(text="Hello")]),
    ]

    path = chat.save_history()
    payload = json.loads(open(path, encoding="utf-8").read())

    assert os.path.dirname(path) == str(tmp_path)
    assert payload["version"] == 1
    assert payload["query"] == ""
    assert len(payload["turns"]) == 2

    restored = make_chatter(history_dir=str(tmp_path / "other"))
    assert restored.load_history(path) == 2
    assert [turn.role for turn in restored.history] == ["user", "model"]
    assert restored.history[0].parts[0].text == "Hi"

    assert restored.load_history(path, append=True) == 2
    assert len(restored.history) == 4


def test_deserialize_skips_unreadable_turns():
    restored = file_module.Chatter._deserialize_history(
        [
            {"role": "user", "parts": [{"text": "Hi"}]},
            {"role": "user", "parts": "not-a-list"},
            "nonsense",
        ]
    )

    assert len(restored) == 1
    assert restored[0].parts[0].text == "Hi"


def test_archive_history_writes_a_timestamped_copy_and_clears(make_chatter, tmp_path):
    chat = make_chatter(history_dir=str(tmp_path))
    chat.history = [types.Content(role="user", parts=[types.Part.from_text(text="Hi")])]

    path = chat.archive_history("daily", clear=True)

    assert os.path.isfile(path)
    assert os.path.basename(path).endswith("_daily.json")
    assert chat.history == []
    assert chat.list_archives() == [os.path.abspath(path)]


def test_talk_to_gemini_passes_system_instruction_and_tools(make_chatter):
    chat = make_chatter()
    factory_models = chat.client.models

    answer = chat.talk_to_gemini("Question?", model="gemini-2.5-pro", system_instruction="Be brief.")

    assert answer == "Answer text."
    call = factory_models.calls[-1]
    assert call["model"] == "gemini-2.5-pro"
    assert call["config"].system_instruction == "Be brief."
    assert len(call["config"].tools) == 2
    assert [turn.role for turn in chat.history] == ["user", "model"]
    assert chat.history[1].parts[0].text == "Answer text."


def test_talk_to_gemini_can_disable_tools(make_chatter):
    chat = make_chatter()

    chat.talk_to_gemini("Hello", enable_tools=False)

    call = chat.client.models.calls[-1]
    assert call["config"].tools is None
    assert call["config"].automatic_function_calling is None
    assert len(chat.history) == 2


def test_automatic_function_calling_history_is_kept(make_chatter):
    chat = make_chatter()
    tool_steps = [
        types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(name="list_articles_tool"))],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="list_articles_tool", response={"result": []}
                    )
                )
            ],
        ),
    ]
    chat.client.models.response = SimpleNamespace(
        text="Grounded answer.", automatic_function_calling_history=tool_steps
    )

    answer = chat.talk_to_gemini("What's the top story?")

    assert answer == "Grounded answer."
    assert len(chat.history) == 3  # two tool steps + the model's final answer
    assert chat.history[-1].parts[0].text == "Grounded answer."


def test_clear_history_leaves_files_alone(make_chatter, tmp_path):
    chat = make_chatter(history_dir=str(tmp_path))
    chat.history = [types.Content(role="user", parts=[types.Part.from_text(text="Hi")])]
    path = chat.save_history()

    chat.clear_history()

    assert chat.history == []
    assert os.path.isfile(path)

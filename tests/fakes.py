"""A stand-in for `file.Chatter` so the API tests never touch the network.

The real `Chatter` needs a Gemini client and a NewsAPI key; the Flask layer only
uses a small part of its surface (`articles`, `fetch_articles`, `talk_to_gemini`,
`get_full_article`, `save_history`, `load_history`). This fake implements that
surface and records what the app asked for, so tests can assert on it.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timezone


def make_articles(topic: str = "Apple", count: int = 3) -> list[dict]:
    return [
        {
            "title": f"{topic} headline {index + 1}",
            "description": f"Short description {index + 1} about {topic}.",
            "url": f"https://example.test/{topic.lower()}/{index + 1}",
            "publishedAt": f"2026-11-2{index}T09:00:00Z",
            "source": {"name": "Test Wire"},
            "content": f"Snippet {index + 1}.",
            "full_text": f"Full body text {index + 1} about {topic}.",
            "text_source": "full_scrape",
        }
        for index in range(count)
    ]


class FakeChatter:
    # Knobs the tests set. `reset()` is called by the workspace fixture.
    fetch_calls: list = []
    calls: list = []
    full_text_calls: list = []
    tool_plan: list = []
    reply: str = "Fake answer about the loaded headlines."
    fail_with: Exception | None = None
    article_count: int = 3
    article_sets: dict = {}

    def __init__(
        self,
        query: str = "",
        page_size: int = 20,
        gemini_api_key: str | None = None,
        news_api_key: str | None = None,
        log_tools: bool = True,
        history_dir: str = "chat_history",
    ) -> None:
        self.query = query
        self.page_size = page_size
        self.history_dir = history_dir
        self.articles: list[dict] = []
        self.turns: list[dict] = []
        self.loaded_from: str | None = None
        if query:  # mirrors the real class: a topic fetches on construction
            self.fetch_articles(query, page_size)

    # -- test bookkeeping -------------------------------------------------
    @classmethod
    def reset(cls) -> None:
        cls.fetch_calls = []
        cls.calls = []
        cls.full_text_calls = []
        cls.tool_plan = []
        cls.reply = "Fake answer about the loaded headlines."
        cls.fail_with = None
        cls.article_count = 3
        cls.article_sets = {}

    # -- the surface app.py uses -----------------------------------------
    def _log_tool(self, tool_name: str, detail: str) -> None:
        pass

    def fetch_articles(self, query: str | None = None, page_size: int | None = None):
        self.__class__.fetch_calls.append((query, page_size))
        topic = ((query if query is not None else self.query) or "").strip()
        if not topic:
            raise ValueError("Give a topic to fetch headlines for.")
        self.query = topic
        self.page_size = int(page_size or self.page_size or 20)
        source = self.__class__.article_sets.get(topic)
        if source is not None:
            self.articles[:] = copy.deepcopy(source[: self.page_size] if page_size else source)
        else:
            self.articles[:] = make_articles(
                topic, min(self.page_size, self.__class__.article_count)
            )
        return self.articles

    def talk_to_gemini(
        self,
        question: str,
        model: str = "gemini-3.5-flash-lite",
        use_history: bool = True,
        content=None,
        enable_tools: bool = True,
        max_remote_calls: int = 5,
        system_instruction: str | None = None,
    ) -> str:
        if self.__class__.fail_with is not None:
            raise self.__class__.fail_with

        self.__class__.calls.append(
            {
                "question": question,
                "model": model,
                "system_instruction": system_instruction,
                "enable_tools": enable_tools,
            }
        )
        if enable_tools:
            for tool_name, detail in self.__class__.tool_plan:
                self._log_tool(tool_name, detail)

        answer = self.__class__.reply
        if use_history:
            self.turns.append({"role": "user", "parts": [{"text": question}]})
            self.turns.append({"role": "model", "parts": [{"text": answer}]})
        return answer

    def get_full_article(self, article_or_title, **kwargs) -> str:
        article = (
            next((a for a in self.articles if a.get("title") == article_or_title), None)
            if isinstance(article_or_title, str)
            else article_or_title
        )
        if not article:
            return ""
        self.__class__.full_text_calls.append(article.get("title"))
        if article.get("full_text"):
            article["text_source"] = "full_scrape"
            return article["full_text"]
        article["text_source"] = "newsapi_fallback"
        return article.get("description") or ""

    def save_history(self, path: str | None = None) -> str:
        target = os.path.abspath(path or os.path.join(self.history_dir, "chatter_history.json"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": self.query,
            "turns": copy.deepcopy(self.turns),
        }
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        return target

    def load_history(self, path: str, *, append: bool = False) -> int:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        turns = payload.get("turns", []) if isinstance(payload, dict) else payload
        if append:
            self.turns.extend(turns)
        else:
            self.turns = list(turns)
        self.loaded_from = os.path.abspath(path)
        return len(turns)

    def clear_history(self) -> None:
        self.turns.clear()

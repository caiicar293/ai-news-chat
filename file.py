# pip install requests trafilatura google-genai
"""News Q&A chatbot on top of Google Gemini, with chat history saved to disk.

Setup
-----
1. Install the dependencies:
       pip install requests trafilatura google-genai
2. Provide your API keys as environment variables:
       GEMINI_API_KEY=...   # https://aistudio.google.com/apikey
       NEWSAPI_KEY=...      # https://newsapi.org/register

Quick start
-----------
    from file import Chatter

    # Fetch today's headlines for a topic (this makes one NewsAPI request).
    chat = Chatter(query="Apple", page_size=20)

    # Optionally show the fetched articles in the console.
    chat.show_articles()

    # Ask Gemini. History is reused by default, so follow-ups keep context.
    print(chat.talk_to_gemini("What is the biggest Apple story today?"))
    print(chat.talk_to_gemini("Summarise that in two bullet points"))

Saving and archiving the chat history
-------------------------------------
    chat.save_history()                         # chat_history/chatter_history.json
    chat.save_history("notes/session-1.json")   # explicit path (folders are created)

    chat.archive_history("apple-news")          # chat_history/20261123-101500_apple-news.json
    chat.archive_history("daily", clear=True)   # archive, then start a fresh conversation

    chat.list_archives()                        # all saved history files in the folder

    archive = "chat_history/20261123-101500_apple-news.json"
    chat.load_history(archive)                  # restore: replaces current history
    chat.load_history(archive, append=True)     # restore: adds on top of current history

    chat.clear_history()                        # drop the in-memory history only

Every saved file is JSON shaped like:
    {"version": 1, "saved_at": "<UTC ISO time>", "query": "Apple", "turns": [...]}
"""

import json
import os
import time
from datetime import datetime, timezone

import requests
import trafilatura
from google import genai
from google.genai import types


def _build_article_tools(
    articles: list[dict],
    get_full_article_fn,
    log_tool_fn,
):
    """Creates standalone tool callables that do not capture Chatter or genai.Client."""

    def list_articles_tool() -> list[dict]:
        """Lists available fetched news articles with their index and summary fields.

        Returns:
            A list of compact article metadata dicts without full scraped text.
        """
        log_tool_fn("list_articles_tool", f"count={len(articles)}")
        return [
            {
                "index": idx,
                "title": art.get("title"),
                "source": (art.get("source") or {}).get("name"),
                "publishedAt": art.get("publishedAt"),
                "description": art.get("description"),
                "has_cached_full_text": bool(art.get("full_text")),
            }
            for idx, art in enumerate(articles)
        ]

    def fetch_article_full_text_tool(index: int, max_chars: int = 12000) -> dict:
        """Fetches and extracts the full text for a single article by 0-based index.

        Args:
            index: Zero-based index of the article from `list_articles_tool`.
            max_chars: Maximum characters of article text to return in the tool output.

        Returns:
            A dict containing the article metadata and extracted or fallback text.
        """
        if not (0 <= index < len(articles)):
            log_tool_fn(
                "fetch_article_full_text_tool",
                f"index={index} -> OUT_OF_RANGE",
            )
            return {
                "error": f"Article index {index} out of range (0..{len(articles) - 1})."
            }

        article = articles[index]
        was_cached = bool(article.get("full_text"))
        text = get_full_article_fn(article) or ""
        source_kind = article.get("text_source", "none")
        cache_state = "cached" if was_cached else source_kind

        log_tool_fn(
            "fetch_article_full_text_tool",
            f"index={index}, state={cache_state}, chars={min(len(text), max_chars)}",
        )

        return {
            "index": index,
            "title": article.get("title"),
            "source": (article.get("source") or {}).get("name"),
            "publishedAt": article.get("publishedAt"),
            "url": article.get("url"),
            "text_source": source_kind,
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
        }

    return [list_articles_tool, fetch_article_full_text_tool]


class Chatter:
    def __init__(
        self,
        query: str = "Apple",
        page_size: int = 20,
        gemini_api_key: str | None = None,
        news_api_key: str | None = None,
        log_tools: bool = True,
        history_dir: str = "chat_history",
    ) -> None:
        resolved_gemini_key = gemini_api_key or os.getenv("GEMINI_API_KEY")
        resolved_news_key = news_api_key or os.getenv("NEWSAPI_KEY")

        self.client = genai.Client(api_key=resolved_gemini_key)

        # Conversation turns as types.Content objects (user/model/tool steps).
        self.history: list[types.Content] = []

        self.log_tools = log_tools

        # Folder used by save_history() / archive_history() / list_archives().
        # Change it per session if you want separate archives, e.g.
        #   Chatter(query="Apple", history_dir="chat_history/apple")
        self.history_dir = history_dir

        # Stored so every archived file records which topic it belongs to.
        self.query = query
        self.page_size = page_size
        self.news_api_key = resolved_news_key
        self._turn_start = time.perf_counter()

        self.data: dict = {"status": "ok", "articles": []}
        self.articles: list[dict] = []

        # An empty query starts a plain chat: `fetch_articles()` can load
        # headlines later (that is what the web app's "Get news" button does).
        if query:
            self.fetch_articles(query, page_size)

    def _log_tool(self, tool_name: str, detail: str) -> None:
        if self.log_tools:
            elapsed_ms = (time.perf_counter() - self._turn_start) * 1000
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts} | +{elapsed_ms:7.1f} ms] TOOL -> {tool_name}: {detail}")

    def fetch_articles(
        self, query: str | None = None, page_size: int | None = None
    ) -> list[dict]:
        """Fetches today's top headlines for `query` and replaces `self.articles`.

        Usage:
            chat = Chatter(query="")          # no network call up front
            chat.fetch_articles("Apple", 10)   # one NewsAPI request
            chat.show_articles()

        Returns:
            The freshly fetched article list.
        """
        topic = (query if query is not None else self.query) or ""
        topic = topic.strip()
        if not topic:
            raise ValueError("Give a topic to fetch headlines for.")

        size = max(1, min(int(page_size or self.page_size or 20), 100))
        url = "https://newsapi.org/v2/top-headlines"
        params = {
            "q": topic,
            "pageSize": size,
            "page": 1,
            "apiKey": self.news_api_key,
        }

        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "ok":
            raise RuntimeError(f"NewsAPI error: {payload.get('message', payload)}")

        self.query = topic
        self.page_size = size
        self.data = payload
        # In-place update keeps the article list identity used by the tools.
        self.articles[:] = payload.get("articles", [])
        return self.articles

    def show_articles(self) -> None:
        for i, article in enumerate(self.articles, start=1):
            source_name = (article.get("source") or {}).get("name")
            print(f"{i}. {article.get('title')}")
            print(f"   Source: {source_name} | {article.get('publishedAt')}")
            print(f"   Author: {article.get('author')}")
            print(f"   Description: {article.get('description')}")
            print(f"   Content: {article.get('content')}")
            print(f"   URL: {article.get('url')}\n")

    def get_article(self, title: str) -> dict | None:
        for article in self.articles:
            if article.get("title") == title:
                return article
        return None

    def get_full_article(
        self,
        article_or_title: dict | str,
        *,
        timeout: int = 15,
        force_refresh: bool = False,
        fallback_to_snippet: bool = True,
    ) -> str | None:
        """Fetches and extracts full article body text from `article['url']`."""
        article = (
            self.get_article(article_or_title)
            if isinstance(article_or_title, str)
            else article_or_title
        )
        if not article:
            return None

        if not force_refresh and article.get("full_text"):
            return article["full_text"]

        url = article.get("url")
        extracted_text: str | None = None

        if url:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            }
            try:
                resp = requests.get(url, headers=headers, timeout=timeout)
                resp.raise_for_status()
                extracted_text = trafilatura.extract(
                    resp.text,
                    url=url,
                    include_comments=False,
                    include_tables=False,
                )
                if extracted_text:
                    extracted_text = extracted_text.strip()
            except requests.RequestException:
                extracted_text = None

        if extracted_text:
            article["full_text"] = extracted_text
            article["text_source"] = "full_scrape"
            return extracted_text

        if fallback_to_snippet:
            fallback = article.get("description") or article.get("content")
            article["text_source"] = "newsapi_fallback"
            return fallback

        return None

    def clear_history(self) -> None:
        """Forgets the in-memory conversation (files on disk are untouched).

        Usage:
            chat.clear_history()
        """
        self.history.clear()

    # ------------------------------------------------------------------
    # Disk persistence / archiving of the conversation history
    #
    # Usage overview:
    #   chat.save_history()                        # snapshot -> <history_dir>/chatter_history.json
    #   chat.save_history("notes/run.json")        # snapshot -> explicit file (dirs created)
    #   chat.archive_history("apple-news")         # timestamped copy, live history kept
    #   chat.archive_history("daily", clear=True)  # timestamped copy, then clear history
    #   chat.list_archives()                       # list every saved history file in the folder
    #   chat.load_history(path)                    # restore: replaces the current history
    #   chat.load_history(path, append=True)       # restore: appends to the current history
    # ------------------------------------------------------------------
    def _serialize_history(self) -> list[dict]:
        """Converts every stored `types.Content` turn into a JSON-safe dict."""
        turns: list[dict] = []
        for item in self.history:
            if hasattr(item, "model_dump"):
                turns.append(item.model_dump(mode="json", exclude_none=True))
            elif isinstance(item, dict):
                turns.append(item)
        return turns

    @staticmethod
    def _deserialize_history(raw_turns: list) -> list[types.Content]:
        """Rebuilds `types.Content` objects from previously serialized turns."""
        restored: list[types.Content] = []
        for item in raw_turns or []:
            if isinstance(item, types.Content):
                restored.append(item)
            elif isinstance(item, dict):
                try:
                    restored.append(types.Content.model_validate(item))
                except Exception:
                    # Skip turns we cannot rebuild instead of losing the whole file.
                    continue
        return restored

    def save_history(self, path: str | None = None) -> str:
        """Writes the current chat history to a JSON file on disk.

        Args:
            path: Destination file. Defaults to `<history_dir>/chatter_history.json`.

        Returns:
            The absolute path of the written file.

        Usage:
            chat.save_history()                       # chat_history/chatter_history.json
            chat.save_history("notes/session-1.json")  # custom path, folders auto-created
            saved = chat.save_history()               # keep the path for later loads
        """
        target = path or os.path.join(self.history_dir, "chatter_history.json")
        target = os.path.abspath(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)

        payload = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": getattr(self, "query", None),
            "turns": self._serialize_history(),
        }

        # Write to a temp file first so a crash cannot leave a corrupt archive.
        tmp_path = f"{target}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target)

        self._log_tool("save_history", f"path={target}, turns={len(payload['turns'])}")
        return target

    def archive_history(
        self,
        label: str | None = None,
        *,
        archive_dir: str | None = None,
        clear: bool = False,
    ) -> str:
        """Archives the chat history into a timestamped file.

        Args:
            label: Optional short name used as a suffix, e.g. "apple-news".
            archive_dir: Directory for archives. Defaults to `self.history_dir`.
            clear: Clear the in-memory history after a successful archive.

        Returns:
            The absolute path of the created archive file.

        Usage:
            # Keep chatting, but leave a timestamped copy behind:
            chat.archive_history("apple-news")

            # Stop here: save the session, then start clean:
            chat.archive_history("daily", clear=True)

            # Send archives to a different folder:
            chat.archive_history("weekly", archive_dir="backups/chats")
        """
        directory = archive_dir or self.history_dir
        safe_label = "".join(
            ch if (ch.isalnum() or ch in "-_") else "-" for ch in (label or "chat")
        ).strip("-_") or "chat"
        filename = f"{time.strftime('%Y%m%d-%H%M%S')}_{safe_label}.json"

        saved_path = self.save_history(os.path.join(directory, filename))
        if clear:
            self.clear_history()
        return saved_path

    def list_archives(self, archive_dir: str | None = None) -> list[str]:
        """Returns every saved history file (snapshots and archives) in `archive_dir`.

        The list is sorted by filename, and archives start with a timestamp, so
        they come out oldest first.

        Usage:
            for path in chat.list_archives():
                print(path)

            newest = chat.list_archives()[-1]
            chat.load_history(newest)
        """
        directory = archive_dir or self.history_dir
        if not os.path.isdir(directory):
            return []
        return sorted(
            os.path.abspath(os.path.join(directory, name))
            for name in os.listdir(directory)
            if name.endswith(".json")
        )

    def load_history(self, path: str, *, append: bool = False) -> int:
        """Loads a saved/archived history file back into memory.

        Args:
            path: Archive file created by `save_history` / `archive_history`.
            append: Keep the current history and append the loaded turns.

        Returns:
            Number of turns restored.

        Usage:
            # Replace the current conversation with an archived one:
            chat.load_history("chat_history/20261123-101500_apple-news.json")

            # Or continue an existing conversation, adding the file on top:
            chat.load_history(path, append=True)

            # Loaded turns are sent to Gemini again on the next question:
            chat.talk_to_gemini("Where were we?")
        """
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)

        raw_turns = payload.get("turns", []) if isinstance(payload, dict) else payload
        restored = self._deserialize_history(raw_turns)

        if append:
            self.history.extend(restored)
        else:
            self.history = restored

        self._log_tool("load_history", f"path={path}, turns={len(restored)}")
        return len(restored)

    def _print_afc_trace(self, response: types.GenerateContentResponse) -> None:
        """Prints every function call and response recorded in automatic function calling history."""
        afc_history = getattr(response, "automatic_function_calling_history", None) or []
        if not afc_history:
            self._log_tool("none", "no tool calls made this turn")
            return

        for step_idx, content in enumerate(afc_history, start=1):
            for part in content.parts or []:
                if part.function_call:
                    fc = part.function_call
                    self._log_tool(
                        "model_requested",
                        f"step={step_idx} fn={fc.name} args={fc.args}",
                    )
                elif part.function_response:
                    fr = part.function_response
                    self._log_tool(
                        "sdk_returned",
                        f"step={step_idx} fn={fr.name}",
                    )

    def talk_to_gemini(
        self,
        question: str,
        model: str = "gemini-3.5-flash-lite",
        use_history: bool = True,
        content: dict | list | str | None = None,
        enable_tools: bool = True,
        max_remote_calls: int = 5,
        system_instruction: str | None = None,
    ) -> str:
        """Sends one question to Gemini and stores the exchange in `self.history`.

        Args:
            question: The question text.
            model: Gemini model name to use.
            use_history: Send (and then store) prior turns. False = one-off question.
            content: Optional dict/list/str sent as extra context before the question,
                e.g. an article dict from `chat.articles[0]`.
            enable_tools: Let the model call the article tools (`list_articles_tool`,
                `fetch_article_full_text_tool`) automatically.
            max_remote_calls: Upper bound on tool round-trips per turn.
            system_instruction: Optional persona/rules sent with the request (not
                stored in the history).

        Returns:
            The model's answer text (empty string if the model returned none).

        Usage:
            chat.talk_to_gemini("What happened today?")
            chat.talk_to_gemini("Summarise this", content=chat.articles[0])
            chat.talk_to_gemini("Hi", use_history=False)      # keep it out of history
            chat.talk_to_gemini("Just answer", enable_tools=False)  # no article tools
        """
        self._turn_start = time.perf_counter()

        if content is not None:
            serialized = (
                json.dumps(content, indent=2)
                if isinstance(content, (dict, list))
                else str(content)
            )
            prompt_text = f"Context:\n{serialized}\n\nQuestion:\n{question}"
        else:
            prompt_text = question

        user_turn = types.Content(
            role="user",
            parts=[types.Part.from_text(text=prompt_text)],
        )

        request_contents: list[types.Content] = (
            [*self.history, user_turn] if use_history else [user_turn]
        )

        tools = (
            _build_article_tools(
                articles=self.articles,
                get_full_article_fn=self.get_full_article,
                log_tool_fn=self._log_tool,
            )
            if enable_tools
            else None
        )

        config = types.GenerateContentConfig(
            tools=tools,
            system_instruction=system_instruction,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=not enable_tools,
                maximum_remote_calls=max_remote_calls,
            )
            if enable_tools
            else None,
        )

        response = self.client.models.generate_content(
            model=model,
            contents=request_contents,
            config=config,
        )

        if self.log_tools and enable_tools:
            self._print_afc_trace(response)

        ai_res = response.text or ""

        if use_history:
            afc_history = getattr(response, "automatic_function_calling_history", None) or []
            if afc_history:
                self.history.extend(afc_history)
            else:
                self.history.append(user_turn)
            self.history.append(
                types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=ai_res)],
                )
            )

        return ai_res
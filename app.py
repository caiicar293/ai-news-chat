"""Flask backend for the news-chat web app.

The browser talks to this server; the server drives the `Chatter` class from
`file.py` (NewsAPI headlines + Google Gemini Q&A) and keeps one Chatter per
browser chat.

Run it with:
    pip install -r requirements.txt
    python app.py                 # -> http://127.0.0.1:5000

Environment (a .env file next to this script is loaded when python-dotenv is
installed):
    GEMINI_API_KEY   https://aistudio.google.com/apikey
    NEWSAPI_KEY      https://newsapi.org/register

API summary
-----------
    GET    /api/health                      server + dependency status
    GET    /api/models                      model ids for the switcher
    GET    /api/chats                       sidebar list
    POST   /api/chats                       {} or {"query": "Apple"} - a chat is chat-only
                                            until headlines are fetched
    GET    /api/chats/<id>                  messages + article summaries
    PATCH  /api/chats/<id>                  {"model": "..."} remember model
    DELETE /api/chats/<id>                  drop a chat (and its saved session)
    POST   /api/chats/<id>/messages         {"question": "...", "model": "..."}
    POST   /api/chats/<id>/news             {"query": "Apple", "page_size": 20}
                                            fetches headlines mid-chat
    GET    /api/chats/<id>/articles         article list (metadata only)
    GET    /api/chats/<id>/articles/<index> one article, full text extracted

Sessions are saved under chat_history/web/<id>/ (chat.json, articles.json and
chatter_history.json) after every turn and restored when the server starts.
Existing chat_history/*.json archives written by the CLI are imported too, and
continuing one writes the new turns back into that same file.

Every error answer is JSON shaped like
    {"error": "<human readable>", "code": "<machine code>", "retry_after": 20?}
so the front end can show a message and, for 429s, a countdown before retrying.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

try:  # optional: load GEMINI_API_KEY / NEWSAPI_KEY from .env
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # python-dotenv not installed - rely on the real environment
    pass

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

try:
    from file import Chatter

    CHATTER_IMPORT_ERROR = ""
except Exception as exc:  # missing requests / trafilatura / google-genai
    Chatter = None  # type: ignore[assignment]
    CHATTER_IMPORT_ERROR = f"{exc.__class__.__name__}: {exc}"


app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# (model id, label) pairs offered by the model switcher in the header.
MODELS: list[tuple[str, str]] = [
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite - fastest"),
    ("gemini-3.5-flash", "Gemini 3.5 Flash - balanced"),
    ("gemini-3.5-pro", "Gemini 3.5 Pro - deepest reasoning"),
    ("gemini-2.5-flash", "Gemini 2.5 Flash"),
    ("gemini-2.5-pro", "Gemini 2.5 Pro"),
]
DEFAULT_MODEL = MODELS[0][0]

# Client-side throttle: two questions closer together than this are rejected
# with a 429 so a double-click cannot burn two Gemini requests.
MIN_SECONDS_BETWEEN_QUESTIONS = 1.0

# Where a chat's session lives: chat.json, articles.json, chatter_history.json.
HISTORY_ROOT = os.path.join("chat_history", "web")
# Archives written by file.py (chat_history/*.json) are imported read-write.
ARCHIVE_DIR = "chat_history"

# Chats live in server memory; the oldest are dropped past this many.
MAX_SESSIONS = 40

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")

SESSIONS: dict[str, "ChatSession"] = {}
SESSIONS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

RATE_LIMIT_HINTS = (
    "429",
    "too many requests",
    "rate limit",
    "ratelimit",
    "resource_exhausted",
    "resource exhausted",
    "quota",
)


def describe_error(exc: Exception) -> tuple[dict, int]:
    """Maps an exception raised by Chatter/Gemini/NewsAPI to (json, status)."""
    detail = (str(exc) or exc.__class__.__name__).strip()
    lowered = detail.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        status = int(status)  # google errors use HTTPStatus enums
    except (TypeError, ValueError):
        status = None

    def payload(message: str, code: str, http_status: int, retry_after=None):
        body = {"error": message, "code": code, "detail": detail[:600]}
        if retry_after is not None:
            body["retry_after"] = retry_after
        return body, http_status

    if status == 429 or any(hint in lowered for hint in RATE_LIMIT_HINTS):
        return payload(
            "Rate limit reached - the API is refusing more requests right now. "
            "Wait a moment and try again.",
            "rate_limited",
            429,
            retry_after=20,
        )

    if status in (401, 403) or any(
        hint in lowered
        for hint in ("api key", "api_key", "apikey", "unauthorized", "permission denied", "invalid key")
    ):
        return payload(
            "The API rejected the credentials. Check GEMINI_API_KEY and "
            "NEWSAPI_KEY in the .env file.",
            "auth",
            502,
        )

    if "timeout" in lowered or "timed out" in lowered:
        return payload("The request timed out. Try again.", "timeout", 504)

    if any(hint in lowered for hint in ("connection", "network", "getaddrinfo", "ssl")):
        return payload(
            "Could not reach the network. Check the connection and retry.",
            "network",
            502,
        )

    return payload(
        "Something went wrong while handling that request. Try again.",
        "server_error",
        502,
    )


def _too_many(message: str, retry_after: float) -> tuple:
    wait = max(1, int(round(retry_after)))
    return (
        jsonify({"error": message, "code": "rate_limited", "retry_after": wait}),
        429,
        {"Retry-After": str(wait)},
    )


def error_response(exc: Exception) -> tuple:
    """describe_error() plus a Retry-After header on 429s."""
    body, status = describe_error(exc)
    response = jsonify(body)
    if body.get("retry_after"):
        response.headers["Retry-After"] = str(int(body["retry_after"]))
    return response, status


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _write_json(path: str, payload: dict) -> None:
    """Atomic JSON write: a crash cannot leave a half-written session file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# One chat = one Chatter instance + the transcript the UI renders
# ---------------------------------------------------------------------------


class ChatSession:
    def __init__(
        self,
        session_id: str,
        query: str = "",
        page_size: int = 20,
        *,
        archive_path: str | None = None,
    ) -> None:
        self.id = session_id
        self.query = query
        self.page_size = page_size
        self.created_at = _now()
        self.updated_at = self.created_at
        self.model = DEFAULT_MODEL
        self.messages: list[dict] = []
        # Set when the chat was imported from a chat_history/*.json archive;
        # new turns are then written back into that file.
        self.archive_path = archive_path

        # A chat starts as a plain conversation. The Chatter (and its NewsAPI
        # request) is built on first use, which is what "Get news" triggers.
        self.chatter = None
        self._articles: list[dict] = []

        self.dir = os.path.join(HISTORY_ROOT, session_id)
        self.chat_path = os.path.join(self.dir, "chat.json")
        self.articles_path = os.path.join(self.dir, "articles.json")
        self.history_path = os.path.join(self.dir, "chatter_history.json")

        self.lock = threading.Lock()  # one Gemini call / news fetch at a time
        self._save_lock = threading.Lock()
        self.last_question_at = 0.0

        # Article-tool calls are recorded per question so the UI can show what
        # the model actually looked up ("read article 2") instead of hiding the
        # trace in the server console.
        self.tools_this_turn: list[dict] = []

    @property
    def articles(self) -> list[dict]:
        return self.chatter.articles if self.chatter is not None else self._articles

    def _ensure_chatter(self):
        """Builds the Chatter on demand and restores its saved state."""
        if self.chatter is not None:
            return self.chatter
        if Chatter is None:  # dependencies missing - surfaces as a 503
            raise RuntimeError(CHATTER_IMPORT_ERROR or "Chatter is unavailable")

        self.chatter = Chatter(
            query="",  # no network call here; headlines are loaded below
            page_size=self.page_size,
            log_tools=False,
            history_dir=self.dir,
        )
        self.chatter._log_tool = self._record_tool
        if self.query:
            self.chatter.query = self.query
        if self._articles:
            self.chatter.articles[:] = self._articles
        for path in (self.archive_path, self.history_path):
            if path and os.path.isfile(path):
                try:
                    self.chatter.load_history(path)
                    break
                except Exception:
                    continue
        return self.chatter

    def fetch_news(self, query: str, page_size: int) -> None:
        """Loads headlines into this chat (the app's "Get news" button)."""
        self._ensure_chatter().fetch_articles(query, page_size)
        self.query = query
        self.page_size = page_size
        self.updated_at = _now()
        self._save()

    def system_instruction(self) -> str:
        """Tells the model what the chat currently has to work with."""
        base = (
            "You are a news assistant in a small web app. Reply in the same "
            "language as the user's latest message, keep answers short, and "
            "name the headline you rely on."
        )
        if not self.articles:
            return base + (
                " No headlines are loaded in this chat right now. If the user "
                "asks about the news, say no news is loaded yet and that they "
                'can press the "Get news" button at the top to fetch a topic. '
                "Other questions can be answered normally."
            )
        return base + (
            f" {len(self.articles)} headlines for {self.query!r} are loaded. "
            "Use list_articles_tool and fetch_article_full_text_tool when a "
            "question needs the detail, and never invent headlines."
        )

    TOOL_NAMES = {"list_articles_tool", "fetch_article_full_text_tool"}

    def _record_tool(self, tool_name: str, detail: str) -> None:
        if tool_name in self.TOOL_NAMES:
            self.tools_this_turn.append({"tool": tool_name, "detail": detail})

    @property
    def title(self) -> str:
        first = next((m["text"] for m in self.messages if m["role"] == "user"), "")
        if first:
            text = " ".join(first.split())
            return text if len(text) <= 60 else text[:60].rstrip() + "..."
        return self.query or "New chat"

    def article_summaries(self) -> list[dict]:
        return [
            {
                "index": index,
                "title": art.get("title"),
                "source": (art.get("source") or {}).get("name"),
                "author": art.get("author"),
                "publishedAt": art.get("publishedAt"),
                "description": art.get("description"),
                "url": art.get("url"),
                "image": art.get("urlToImage"),
                "has_full_text": bool(art.get("full_text")),
            }
            for index, art in enumerate(self.articles)
        ]

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "query": self.query,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
            "model": self.model,
            "article_count": len(self.articles),
        }

    def detail(self) -> dict:
        data = self.summary()
        data["messages"] = list(self.messages)
        data["articles"] = self.article_summaries()
        return data

    def _save(self) -> None:
        """Writes the session into chat_history so it survives a restart."""
        with self._save_lock:
            os.makedirs(self.dir, exist_ok=True)
            _write_json(
                self.chat_path,
                {
                    "version": 1,
                    "id": self.id,
                    "title": self.title,
                    "query": self.query,
                    "page_size": self.page_size,
                    "model": self.model,
                    "created_at": self.created_at,
                    "updated_at": self.updated_at,
                    "archive_path": self.archive_path,
                    "messages": self.messages,
                },
            )
            if self.chatter is not None:
                try:
                    self.chatter.save_history(self.history_path)
                    if self.archive_path:
                        self.chatter.save_history(self.archive_path)
                except Exception as exc:  # a failed save must not kill the answer
                    print(f"WARNING: could not save chat history: {exc}", file=sys.stderr)
            if self.articles:
                _write_json(
                    self.articles_path,
                    {
                        "query": self.query,
                        "page_size": self.page_size,
                        "articles": self.articles,
                    },
                )

    @classmethod
    def restore(cls, data: dict, articles: list | None = None) -> "ChatSession":
        """Rebuilds a session from its saved chat.json + articles.json."""
        session = cls(
            str(data.get("id") or uuid.uuid4().hex[:12]),
            query=str(data.get("query") or ""),
            page_size=max(1, min(_to_int(data.get("page_size"), 20), 100)),
            archive_path=data.get("archive_path") or None,
        )
        session.created_at = str(data.get("created_at") or session.created_at)
        session.updated_at = str(data.get("updated_at") or session.updated_at)
        session.model = str(data.get("model") or DEFAULT_MODEL)
        if isinstance(data.get("messages"), list):
            session.messages = data["messages"]
        session._articles = [art for art in (articles or []) if isinstance(art, dict)]
        return session


def _messages_from_turns(turns: list, at: str) -> list[dict]:
    """Pulls the plain Q&A out of serialized Gemini turns (for old archives)."""
    messages: list[dict] = []
    for turn in turns or []:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "model"):
            continue
        text = " ".join(
            str(part.get("text") or "").strip()
            for part in (turn.get("parts") or [])
            if isinstance(part, dict) and part.get("text")
        ).strip()
        if not text:
            continue
        entry = {"role": turn["role"], "text": text, "at": at}
        if turn["role"] == "model":
            entry["model"] = None
            entry["tools"] = []
        messages.append(entry)
    return messages


def _session_from_archive(path: str, session_id: str) -> ChatSession:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError("not a history file")

    saved_at = str(payload.get("saved_at") or _now())
    session = ChatSession(
        session_id,
        query=str(payload.get("query") or ""),
        archive_path=os.path.abspath(path),
    )
    session.created_at = saved_at
    session.updated_at = saved_at
    session.messages = _messages_from_turns(payload.get("turns") or [], saved_at)
    if not session.messages:
        raise ValueError("empty history")
    return session


def _load_saved_sessions() -> None:
    """Restores the sidebar from chat_history/ when the server starts."""
    chat_files: list[tuple[float, str]] = []
    try:
        entries = list(os.scandir(HISTORY_ROOT)) if os.path.isdir(HISTORY_ROOT) else []
    except OSError:
        entries = []
    for entry in entries:
        path = os.path.join(entry.path, "chat.json")
        if not os.path.isfile(path):
            continue
        try:
            chat_files.append((os.path.getmtime(path), path))
        except OSError:
            continue
    chat_files.sort(reverse=True)

    restored = 0
    with SESSIONS_LOCK:
        for _mtime, path in chat_files[:MAX_SESSIONS]:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            articles: list = []
            articles_path = os.path.join(os.path.dirname(path), "articles.json")
            if os.path.isfile(articles_path):
                try:
                    with open(articles_path, "r", encoding="utf-8") as fh:
                        articles = (json.load(fh) or {}).get("articles") or []
                except (OSError, ValueError, AttributeError):
                    articles = []
            try:
                session = ChatSession.restore(data, articles=articles)
            except (TypeError, ValueError):
                continue
            if session.id not in SESSIONS:
                SESSIONS[session.id] = session
                restored += 1

        try:
            archives = sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*.json")))
        except OSError:
            archives = []
        for path in archives:
            session_id = "a" + hashlib.sha1(
                os.path.abspath(path).encode("utf-8")
            ).hexdigest()[:11]
            if session_id in SESSIONS or len(SESSIONS) >= MAX_SESSIONS:
                continue
            try:
                session = _session_from_archive(path, session_id)
            except (OSError, ValueError):
                continue
            SESSIONS[session.id] = session
            restored += 1

    if restored:
        print(f"Restored {restored} saved chat(s) from {ARCHIVE_DIR}/")


def _prune_sessions() -> None:
    """Keeps memory bounded by dropping the least recently used chats."""
    excess = len(SESSIONS) - MAX_SESSIONS
    if excess <= 0:
        return
    oldest = sorted(SESSIONS.items(), key=lambda kv: kv[1].updated_at)[:excess]
    for chat_id, _ in oldest:
        SESSIONS.pop(chat_id, None)


def _get_session(chat_id: str) -> ChatSession | None:
    with SESSIONS_LOCK:
        return SESSIONS.get(chat_id)


def _require_chatter():
    """Returns an error response when the Python dependencies are missing."""
    if Chatter is None:
        return (
            jsonify(
                {
                    "error": "The backend dependencies are not installed for "
                    f'{sys.executable}. Run: "{sys.executable}" -m pip install '
                    "-r requirements.txt",
                    "code": "deps_missing",
                    "python": sys.executable,
                    "detail": CHATTER_IMPORT_ERROR,
                }
            ),
            503,
        )
    return None


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Meta endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": not CHATTER_IMPORT_ERROR,
            "error": CHATTER_IMPORT_ERROR or None,
            "python": sys.executable,
            "default_model": DEFAULT_MODEL,
        }
    )


@app.get("/api/models")
def list_models():
    return jsonify(
        {
            "models": [{"id": mid, "label": label} for mid, label in MODELS],
            "default": DEFAULT_MODEL,
        }
    )


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------


@app.get("/api/chats")
def list_chats():
    with SESSIONS_LOCK:
        chats = [session.summary() for session in SESSIONS.values()]
    chats.sort(key=lambda chat: chat["updated_at"], reverse=True)
    return jsonify({"chats": chats})


@app.post("/api/chats")
def create_chat():
    missing = _require_chatter()
    if missing:
        return missing

    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or "").strip()
    page_size = max(1, min(_to_int(payload.get("page_size") or 20, 20), 100))

    # No topic is required: a new chat is a plain conversation until the user
    # presses "Get news" and headlines are fetched into it.
    session_id = uuid.uuid4().hex[:12]
    session = ChatSession(session_id, query=query, page_size=page_size)

    with SESSIONS_LOCK:
        _prune_sessions()
        SESSIONS[session_id] = session
    session._save()
    return jsonify(session.detail()), 201


@app.get("/api/chats/<chat_id>")
def get_chat(chat_id: str):
    session = _get_session(chat_id)
    if session is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404
    return jsonify(session.detail())


@app.patch("/api/chats/<chat_id>")
def update_chat(chat_id: str):
    session = _get_session(chat_id)
    if session is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404

    payload = request.get_json(silent=True) or {}
    model = str(payload.get("model") or "").strip()
    if not model:
        return jsonify({"error": "No model given.", "code": "bad_model"}), 400
    if not MODEL_ID_RE.match(model):
        return jsonify({"error": "That model name is not valid.", "code": "bad_model"}), 400

    session.model = model
    session.updated_at = _now()
    session._save()
    return jsonify({"chat": session.summary()})


@app.delete("/api/chats/<chat_id>")
def delete_chat(chat_id: str):
    with SESSIONS_LOCK:
        removed = SESSIONS.pop(chat_id, None)
    if removed is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404
    # Drop the saved session too, otherwise it reappears on the next start.
    shutil.rmtree(removed.dir, ignore_errors=True)
    return jsonify({"deleted": chat_id})


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------


@app.post("/api/chats/<chat_id>/messages")
def post_message(chat_id: str):
    session = _get_session(chat_id)
    if session is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404

    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Type a question first.", "code": "empty"}), 400
    if len(question) > 8000:
        return jsonify(
            {"error": "That question is too long (8000 characters max).", "code": "too_long"}
        ), 400

    model = str(payload.get("model") or session.model or DEFAULT_MODEL).strip()
    if not MODEL_ID_RE.match(model):
        return jsonify({"error": "That model name is not valid.", "code": "bad_model"}), 400

    if not session.lock.acquire(blocking=False):
        return _too_many("Still answering the previous question - one moment.", 2)

    try:
        since_last = time.monotonic() - session.last_question_at
        if since_last < MIN_SECONDS_BETWEEN_QUESTIONS:
            wait = MIN_SECONDS_BETWEEN_QUESTIONS - since_last
            return _too_many(f"One question at a time, please. Retry in {wait:.1f}s.", wait)
        session.last_question_at = time.monotonic()
        session.tools_this_turn = []

        chatter = session._ensure_chatter()
        answer = chatter.talk_to_gemini(
            question,
            model=model,
            system_instruction=session.system_instruction(),
        )
        tools_used = list(session.tools_this_turn)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.lock.release()

    now = _now()
    session.messages.append({"role": "user", "text": question, "at": now})
    session.messages.append(
        {
            "role": "model",
            "text": answer or "",
            "at": _now(),
            "model": model,
            "tools": tools_used,
        }
    )
    session.model = model
    session.updated_at = now
    session._save()

    return jsonify(
        {
            "answer": answer or "",
            "model": model,
            "tools": tools_used,
            "messages": session.messages,
            "chat": session.summary(),
        }
    )


# ---------------------------------------------------------------------------
# Articles: the user can browse them and read one in the app
# ---------------------------------------------------------------------------


@app.get("/api/chats/<chat_id>/articles")
def list_articles(chat_id: str):
    session = _get_session(chat_id)
    if session is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404
    return jsonify(
        {"articles": session.article_summaries(), "query": session.query}
    )


@app.post("/api/chats/<chat_id>/news")
def fetch_news(chat_id: str):
    """Fetches headlines for a topic into an existing chat ("Get news")."""
    session = _get_session(chat_id)
    if session is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404

    payload = request.get_json(silent=True) or {}
    query = str(payload.get("query") or "").strip()
    if not query:
        return jsonify(
            {"error": "Give me a topic to fetch headlines for.", "code": "empty"}
        ), 400
    page_size = max(1, min(_to_int(payload.get("page_size") or 20, 20), 100))

    if not session.lock.acquire(blocking=False):
        return _too_many("Still working on the previous request - one moment.", 2)
    try:
        session.fetch_news(query, page_size)
    except Exception as exc:
        return error_response(exc)
    finally:
        session.lock.release()

    return jsonify(
        {"chat": session.detail(), "articles": session.article_summaries()}
    )


@app.get("/api/chats/<chat_id>/articles/<int:index>")
def read_article(chat_id: str, index: int):
    session = _get_session(chat_id)
    if session is None:
        return jsonify({"error": "That chat no longer exists.", "code": "not_found"}), 404

    articles = session.articles
    if not 0 <= index < len(articles):
        return jsonify({"error": "That article is not in this chat.", "code": "not_found"}), 404

    article = articles[index]
    try:
        text = session._ensure_chatter().get_full_article(article) or ""
    except Exception as exc:
        return error_response(exc)

    try:  # cache the extracted text for the next open / restart
        session._save()
    except Exception:
        pass

    return jsonify(
        {
            "index": index,
            "title": article.get("title"),
            "source": (article.get("source") or {}).get("name"),
            "author": article.get("author"),
            "publishedAt": article.get("publishedAt"),
            "url": article.get("url"),
            "image": article.get("urlToImage"),
            "text": text,
            "characters": len(text),
            "text_source": article.get("text_source", "none"),
        }
    )


# ---------------------------------------------------------------------------
# Fallbacks: always answer JSON on /api/* so the front end never parses HTML
# ---------------------------------------------------------------------------


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    if request.path.startswith("/api/"):
        code = (exc.name or "error").lower().replace(" ", "_")
        return (
            jsonify({"error": exc.description or exc.name, "code": code}),
            exc.code or 500,
        )
    return exc


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    return error_response(exc)


# Rebuild the sidebar from chat_history/ as soon as the server starts.
_load_saved_sessions()


if __name__ == "__main__":
    if CHATTER_IMPORT_ERROR:
        print("WARNING: could not import Chatter from file.py ->", CHATTER_IMPORT_ERROR)
        print("Running interpreter:", sys.executable)
        print(f'Install the dependencies for it: "{sys.executable}" -m pip install -r requirements.txt')
    port = int(os.getenv("PORT", "5000"))
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=port, debug=True)

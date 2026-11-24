# News Chat

A local web app for talking to Gemini about today's news. Start chatting
immediately, pull today's headlines for any topic with one button, then ask
about them — or open the reader and read the full articles yourself.

Flask backend · plain-JavaScript frontend · Google Gemini · NewsAPI ·
[trafilatura](https://trafilatura.readthedocs.io/) for article text.

## Features

- **Chat first, news optional.** A new chat needs no title or topic. Nothing is
  fetched from NewsAPI until you ask for headlines, and the assistant answers
  normally in the meantime.
- **“Get news” button.** Fetches today's headlines for a topic into the current
  chat (button then reads **Read articles N**). The model gets the same
  headlines you do, through two tools: `list_articles_tool` and
  `fetch_article_full_text_tool`.
- **Grounded, honest answers.** Every turn tells the model how many headlines
  are loaded and for which topic. With none loaded it points at “Get news”
  instead of claiming it cannot see articles; with headlines it is told never
  to invent them.
- **Reader pane.** Browse the fetched headlines and read any article in full,
  with extracted text cached on disk. Falls back to the NewsAPI snippet when a
  site blocks extraction, and labels it as such.
- **Sessions survive restarts.** Transcripts, headlines and Gemini history are
  written to `chat_history/` after every turn. Existing CLI archives are
  imported into the sidebar and continued in place.
- **Model switcher.** Pick a Gemini model from the header (or type a custom
  model id); the choice is remembered per chat.
- **Readable failures.** Errors come back as JSON with a stable `code`; rate
  limits include `retry_after` and the UI counts down before retrying.

## Quick start

Requires **Python 3.10+** (the code uses `str | None` annotations).

```bash
git clone <https://github.com/caiicar293/ai-news-chat>
cd "AI news"

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Put your keys in `.env`:

```dotenv
GEMINI_API_KEY=...   # https://aistudio.google.com/apikey
NEWSAPI_KEY=...      # https://newsapi.org/register
```

Then run:

```bash
python app.py               # http://127.0.0.1:5000
```

If the keys are missing the chat still starts, but model and headline requests
fail with a clear “check GEMINI_API_KEY and NEWSAPI_KEY” message.

## Using the app

1. **Just type.** The composer works without a topic; the first message names
   the chat.
2. **Get news.** Click it, enter a topic (e.g. `Apple`), choose how many
   headlines (1–100), and submit. Headlines are added to *this* chat.
3. **Ask.** “What's the biggest story today?”, “What does the lawsuit claim?”,
   “Which headline is about jobs?” The model calls the article tools as needed.
4. **Read.** Press **Read articles N** to open the list, then click a headline
   for the full text.
5. **Come back later.** Close the server, start it again — the sidebar and the
   conversations are restored from `chat_history/`.

The model answers in the language of your latest message.

## Command-line use

`file.py` is also usable on its own, without the web app:

```python
from file import Chatter

chat = Chatter(query="Apple", page_size=20)   # one NewsAPI request
chat.show_articles()
print(chat.talk_to_gemini("What's the biggest Apple story today?"))
print(chat.talk_to_gemini("Summarise that in two bullet points"))

chat.save_history()                           # chat_history/chatter_history.json
chat.archive_history("apple-news")            # timestamped copy
chat.load_history("chat_history/20261123-101500_apple-news.json")
```

An empty query starts without a network call, and `fetch_articles("Tesla", 10)`
loads headlines later — that is exactly what the “Get news” button does.

## HTTP API

| Method | Path | Body / purpose |
| --- | --- | --- |
| `GET` | `/api/health` | dependency + interpreter status |
| `GET` | `/api/models` | model ids for the switcher |
| `GET` | `/api/chats` | sidebar list |
| `POST` | `/api/chats` | `{}` or `{"query": "Apple"}`; a chat is chat-only until headlines are fetched |
| `GET` | `/api/chats/<id>` | messages + article summaries |
| `PATCH` | `/api/chats/<id>` | `{"model": "..."}` |
| `DELETE` | `/api/chats/<id>` | drop the chat and its saved session |
| `POST` | `/api/chats/<id>/messages` | `{"question": "...", "model": "..."}` |
| `POST` | `/api/chats/<id>/news` | `{"query": "Apple", "page_size": 20}` |
| `GET` | `/api/chats/<id>/articles` | headline metadata |
| `GET` | `/api/chats/<id>/articles/<index>` | one article, full text extracted |

Errors are always JSON: `{"error": "...", "code": "...", "retry_after": 20?}`.

## How it works

`app.py` keeps one `ChatSession` per chat and builds the underlying `Chatter`
(from `file.py`) lazily, so creating a chat makes no network call. Each turn
sends a system instruction that states either “no headlines are loaded” or
“N headlines for 'X' are loaded”, which is what keeps the assistant from
pretending it has articles it does not have. Article tools receive a compact
metadata list; full text is fetched and extracted only when the model (or you,
via the reader) asks for it, then cached in `articles.json`.

## Where your chats live

```
chat_history/
├── 20261123-170525_apple-news.json     # CLI archives: imported read-write
└── web/
    └── 9f3c1a2b7d4e/
        ├── chat.json                   # transcript, topic, model, timestamps
        ├── articles.json               # fetched headlines + extracted text
        └── chatter_history.json        # Gemini turns, tool calls included
```

Sessions are restored when the server starts (at most `MAX_SESSIONS` chats are
kept in memory). Deleting a chat deletes its folder, so it does not reappear.
Continuing an imported CLI archive writes the new turns back into that same
file.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `GEMINI_API_KEY` | — | Google AI Studio key |
| `NEWSAPI_KEY` | — | NewsAPI key |
| `HOST` | `127.0.0.1` | bind address (`app.py`) |
| `PORT` | `5000` | port (`app.py`) |

`.env` is loaded automatically when `python-dotenv` is installed.

Tunables in `app.py`: `HISTORY_ROOT` (`chat_history/web`), `ARCHIVE_DIR`
(`chat_history`), `MAX_SESSIONS` (40), `MIN_SECONDS_BETWEEN_QUESTIONS` (1s),
`DEFAULT_MODEL`, and the `MODELS` switcher list.

## Testing

Two layers: a fast offline suite, and an opt-in accuracy eval that spends real
Gemini requests.

```bash
pip install -r requirements-dev.txt

python -m pytest              # offline: no network, no API keys
python -m pytest -m eval -s   # live accuracy eval (needs GEMINI_API_KEY)
```

The eval is deselected by default through `pytest.ini`.

### Offline suite — 50 tests

| File | Covers |
| --- | --- |
| `tests/test_chatter.py` | no fetch for an empty query, page-size clamping, in-place article identity, tool metadata/bounds/truncation, extraction cache and snippet fallback, history save/load/archive, tool + AFC config |
| `tests/test_api.py` | chat creation without a topic, chatting with no headlines, system-instruction switching, news validation, article reads, model switch, both 429 paths, provider-error translation |
| `tests/test_sessions.py` | files on disk, restart restore (messages, topic, headlines, Gemini history), delete cleanup, CLI archive import + write-back, corrupt-file tolerance |
| `tests/test_accuracy_grader.py` | the grading rules themselves: claim support, invented numbers across locales, refusal detection, citation, expected facts |

### Accuracy eval — 10 prompts

`tests/test_accuracy.py` drives the real Flask endpoint and the real
`Chatter`, but injects a frozen corpus (`tests/accuracy_corpus.py`: 7 synthetic
articles with full text) so no NewsAPI call is needed and the ground truth is
known. Each answer is scored by `tests/accuracy_grader.py` for:

- expected facts (e.g. the price answer must contain `0.238`)
- whether it names a loaded headline
- which tools it called (`fetch_article_full_text_tool` for detail questions,
  none for a greeting)
- refusal correctness for a story that is not loaded, and for another topic
- unsupported claims and invented numbers relative to the corpus

Gates live in `tests/test_accuracy.py` and can be tuned:

```bash
EVAL_GATE_FACT=0.9 EVAL_GATE_REFUSAL=1.0 python -m pytest -m eval -s
EVAL_MODEL=gemini-2.5-flash EVAL_DELAY=3 python -m pytest -m eval -s
EVAL_REPORT=scorecard.json python -m pytest -m eval -s   # also write JSON
```

The eval retries transient 429/503 responses and skips with an explicit
“not an accuracy result” message if quota or an outage prevents measurement.

### Latest results

`2026-11-24`, `gemini-3.5-flash-lite`, one run:

| Metric | Result |
| --- | --- |
| Expected-fact recall | 1.00 (5/5 fact-checked prompts) |
| Refusal accuracy | 1.00 (2/2) |
| Tool-use compliance | 1.00 (3/3) |
| Names a loaded headline | 1.00 (6/6) |
| Unsupported-claim rate | 0.04 (gate ≤ 0.34) |
| Invented numbers | 0 |
| Latency | p50 1.90 s, max 7.72 s |
| Prompts passing all checks | 10/10 |
| Offline suite | 50 passed |

These are a small, synthetic regression harness — not a benchmark. The corpus
is seven articles, the graders are lexical (answers that paraphrase heavily can
look unsupported), and a single run does not establish that the model never
hallucinates.

## Project layout

```
.
├── app.py                     Flask app, sessions, persistence, API
├── file.py                    Chatter: NewsAPI, Gemini, tools, archives
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── .env.example
├── templates/index.html
├── static/
│   ├── app.js
│   └── style.css
├── tests/
│   ├── conftest.py            isolated chat_history + FakeChatter
│   ├── fakes.py
│   ├── test_chatter.py
│   ├── test_api.py
│   ├── test_sessions.py
│   ├── accuracy_corpus.py
│   ├── accuracy_grader.py
│   ├── test_accuracy_grader.py
│   └── test_accuracy.py
└── chat_history/              runtime data, git-ignored
```

## Notes and limitations

- **Local tool, no authentication.** It binds to `127.0.0.1` by default and has
  no login. Do not expose it publicly as-is.
- **NewsAPI free tier** is limited (localhost-only for development keys, a
  limited request window, and occasional mojibake in titles).
- **Gemini quota** can refuse or rate-limit requests; the UI surfaces this and
  the eval treats it as “not measured”.
- **Empty chats persist** too: a “New chat” you never type in is still in the
  sidebar after a restart.
- The model's answer quality depends on the fetched headlines and on the
  article site allowing text extraction.

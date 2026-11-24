"""Sessions must survive a restart and stay in chat_history/."""

from __future__ import annotations

import json
import os

import app as app_module
from fakes import FakeChatter


def restart_sessions():
    """Simulates a fresh server process loading chat_history/ again."""
    app_module.SESSIONS.clear()
    app_module._load_saved_sessions()


def create_chat(client, **payload):
    response = client.post("/api/chats", json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def write_archive(path, query="Apple", turns=None):
    payload = {
        "version": 1,
        "saved_at": "2026-11-20T10:00:00+00:00",
        "query": query,
        "turns": turns
        or [
            {"role": "user", "parts": [{"text": "What happened?"}]},
            {"role": "model", "parts": [{"text": "Something did."}]},
        ],
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def test_turn_and_news_are_written_to_disk(client, workspace):
    chat = create_chat(client)
    client.post(f"/api/chats/{chat['id']}/messages", json={"question": "Hello"})
    client.post(f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 3})

    folder = workspace / "chat_history" / "web" / chat["id"]
    assert (folder / "chat.json").is_file()
    assert (folder / "articles.json").is_file()
    assert (folder / "chatter_history.json").is_file()

    saved = json.loads((folder / "chat.json").read_text(encoding="utf-8"))
    assert saved["query"] == "Apple"
    assert [m["role"] for m in saved["messages"]] == ["user", "model"]

    articles = json.loads((folder / "articles.json").read_text(encoding="utf-8"))
    assert len(articles["articles"]) == 3


def test_session_is_restored_after_restart(client):
    chat = create_chat(client)
    client.post(f"/api/chats/{chat['id']}/messages", json={"question": "Hello"})
    client.post(f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 3})

    restart_sessions()

    listed = client.get("/api/chats").get_json()["chats"]
    assert [c["id"] for c in listed] == [chat["id"]]

    restored = client.get(f"/api/chats/{chat['id']}").get_json()
    assert restored["title"] == "Hello"
    assert restored["query"] == "Apple"
    assert restored["article_count"] == 3
    assert [m["text"] for m in restored["messages"]][0] == "Hello"

    # The restored chat keeps talking, and its Gemini history was reloaded.
    follow_up = client.post(
        f"/api/chats/{chat['id']}/messages", json={"question": "And then?"}
    )
    assert follow_up.status_code == 200
    assert len(follow_up.get_json()["messages"]) == 4
    session = app_module.SESSIONS[chat["id"]]
    assert session.chatter.loaded_from == str(
        os.path.join(session.dir, "chatter_history.json")
    )


def test_empty_chat_is_restored_too(client):
    chat = create_chat(client)

    restart_sessions()

    restored = client.get(f"/api/chats/{chat['id']}").get_json()
    assert restored["title"] == "New chat"
    assert restored["message_count"] == 0


def test_delete_removes_the_folder_and_the_session(client):
    chat = create_chat(client)
    client.post(f"/api/chats/{chat['id']}/messages", json={"question": "Hello"})
    folder = os.path.join(app_module.HISTORY_ROOT, chat["id"])
    assert os.path.isdir(folder)

    deleted = client.delete(f"/api/chats/{chat['id']}")
    assert deleted.status_code == 200
    assert not os.path.exists(folder)

    restart_sessions()
    assert client.get("/api/chats").get_json()["chats"] == []


def test_cli_archive_is_imported_and_updated_in_place(client, workspace):
    archive = workspace / "chat_history" / "20261120-100000_apple.json"
    write_archive(str(archive))

    restart_sessions()

    listed = client.get("/api/chats").get_json()["chats"]
    imported = [chat for chat in listed if chat["id"].startswith("a")]
    assert len(imported) == 1
    session_id = imported[0]["id"]
    assert imported[0]["message_count"] == 2
    assert imported[0]["query"] == "Apple"

    answer = client.post(
        f"/api/chats/{session_id}/messages", json={"question": "What else?"}
    )
    assert answer.status_code == 200

    saved = json.loads(archive.read_text(encoding="utf-8"))
    assert len(saved["turns"]) == 4
    assert saved["turns"][2]["parts"][0]["text"] == "What else?"
    assert saved["turns"][3]["role"] == "model"


def test_corrupt_session_files_are_skipped(client, workspace):
    good = create_chat(client)
    broken = workspace / "chat_history" / "web" / "broken"
    broken.mkdir(parents=True)
    (broken / "chat.json").write_text("{not json", encoding="utf-8")

    restart_sessions()

    listed = client.get("/api/chats").get_json()["chats"]
    assert [chat["id"] for chat in listed] == [good["id"]]


def test_model_choice_survives_restart(client):
    chat = create_chat(client)
    client.patch(f"/api/chats/{chat['id']}", json={"model": "gemini-3.5-pro"})

    restart_sessions()

    restored = client.get(f"/api/chats/{chat['id']}").get_json()
    assert restored["model"] == "gemini-3.5-pro"


def test_restored_chat_uses_saved_headlines_without_refetching(client):
    chat = create_chat(client)
    client.post(f"/api/chats/{chat['id']}/news", json={"query": "Apple", "page_size": 2})
    FakeChatter.fetch_calls.clear()

    restart_sessions()
    client.post(f"/api/chats/{chat['id']}/messages", json={"question": "Top story?"})

    assert FakeChatter.fetch_calls == []
    session = app_module.SESSIONS[chat["id"]]
    assert len(session.articles) == 2

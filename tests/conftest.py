from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))
for path in (ROOT, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import app as app_module  # noqa: E402
from fakes import FakeChatter  # noqa: E402


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Points the app at a throwaway chat_history/ and swaps in FakeChatter."""
    monkeypatch.setattr(app_module, "HISTORY_ROOT", str(tmp_path / "chat_history" / "web"))
    monkeypatch.setattr(app_module, "ARCHIVE_DIR", str(tmp_path / "chat_history"))
    monkeypatch.setattr(app_module, "MIN_SECONDS_BETWEEN_QUESTIONS", 0.0)
    monkeypatch.setattr(app_module, "Chatter", FakeChatter)
    app_module.SESSIONS.clear()
    FakeChatter.reset()
    yield tmp_path
    app_module.SESSIONS.clear()


@pytest.fixture()
def client(workspace):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as test_client:
        yield test_client

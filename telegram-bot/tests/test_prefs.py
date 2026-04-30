"""Tests for the per-chat preference store."""

from __future__ import annotations

import json
from pathlib import Path

from fitt_telegram_bot.prefs import DEFAULT_ALIAS, DEFAULT_SESSION, PrefsStore


def test_missing_file_uses_defaults(tmp_path: Path) -> None:
    store = PrefsStore(tmp_path / "prefs.json")
    prefs = store.get(12345)
    assert prefs.chat_id == 12345
    assert prefs.alias == DEFAULT_ALIAS
    assert prefs.session_id == DEFAULT_SESSION


def test_set_alias_persists(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    store = PrefsStore(path)
    store.set_alias(12345, "fitt-smart")

    reloaded = PrefsStore(path)
    assert reloaded.get(12345).alias == "fitt-smart"
    assert reloaded.get(12345).session_id == DEFAULT_SESSION


def test_set_session_persists(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    store = PrefsStore(path)
    store.set_session(12345, "retroai")

    reloaded = PrefsStore(path)
    assert reloaded.get(12345).session_id == "retroai"
    assert reloaded.get(12345).alias == DEFAULT_ALIAS


def test_multiple_chats_isolated(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    store = PrefsStore(path)
    store.set_alias(1, "fitt-smart")
    store.set_session(2, "retroai")

    reloaded = PrefsStore(path)
    assert reloaded.get(1).alias == "fitt-smart"
    assert reloaded.get(2).session_id == "retroai"
    # Chat 1 didn't touch session, chat 2 didn't touch alias.
    assert reloaded.get(1).session_id == DEFAULT_SESSION
    assert reloaded.get(2).alias == DEFAULT_ALIAS


def test_corrupted_json_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    path.write_text("not valid json", encoding="utf-8")
    store = PrefsStore(path)
    # Should not raise. Returns defaults.
    assert store.get(1).alias == DEFAULT_ALIAS


def test_atomic_write_produces_valid_json(tmp_path: Path) -> None:
    path = tmp_path / "prefs.json"
    store = PrefsStore(path)
    for i in range(10):
        store.set_alias(i, f"alias-{i}")
    # Re-read the file and parse: must be valid JSON all the way
    # through (no half-written files).
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["chats"]) == 10

"""Tests for the pure handler layer.

These tests never go through python-telegram-bot. They call the
handler functions directly with a FakeBot that records messages and
edits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway.sessions import SessionRegistry

from fitt_telegram_bot import handlers
from fitt_telegram_bot.handlers import IncomingUpdate, Services
from fitt_telegram_bot.prefs import PrefsStore


@dataclass
class FakeBot:
    sent: list[tuple[int, str]] = field(default_factory=list)
    edits: list[tuple[int, int, str]] = field(default_factory=list)

    async def send_message(self, *, chat_id: int, text: str):
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": 1000 + len(self.sent)})()

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))


class FakeGateway:
    def __init__(self, deltas: list[str], aliases: list[str] | None = None) -> None:
        self._deltas = deltas
        self._aliases = aliases or ["fitt-default", "fitt-smart", "fitt-fast"]
        self.seen_calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        alias: str,
        session_id: str,
    ) -> AsyncIterator[str]:
        self.seen_calls.append({"messages": messages, "alias": alias, "session_id": session_id})
        for d in self._deltas:
            yield d

    async def list_aliases(self) -> list[str]:
        return self._aliases


def _services(
    tmp_path: Path,
    *,
    allowlist: frozenset[int] = frozenset({42}),
    gateway: FakeGateway | None = None,
) -> Services:
    prefs = PrefsStore(tmp_path / "prefs.json")
    sessions = SessionRegistry(tmp_path / "sessions")
    sessions.ensure_main()
    return Services(
        gateway=gateway or FakeGateway(["Hello ", "world"]),
        prefs=prefs,
        sessions=sessions,
        allowlist=allowlist,
    )


# ---------- allowlist ---------------------------------------------


def test_allowlist_accept(tmp_path: Path) -> None:
    svc = _services(tmp_path, allowlist=frozenset({42}))
    assert handlers.is_allowed(svc, 42)
    assert not handlers.is_allowed(svc, 99)


# ---------- /start and /help --------------------------------------


async def test_start_shows_current_prefs(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_start(bot, IncomingUpdate(user_id=42, chat_id=100), svc)
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "fitt-default" in text
    assert "main" in text


async def test_help_lists_commands(tmp_path: Path) -> None:
    bot = FakeBot()
    await handlers.handle_help(bot, IncomingUpdate(user_id=42, chat_id=100))
    text = bot.sent[0][1]
    assert "/session" in text
    assert "/model" in text


# ---------- text message ------------------------------------------


async def test_text_message_forwards_with_current_prefs(tmp_path: Path) -> None:
    gw = FakeGateway(["Hel", "lo"])
    svc = _services(tmp_path, gateway=gw)
    svc.prefs.set_alias(100, "fitt-smart")

    bot = FakeBot()
    await handlers.handle_text(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, text="hi there"),
        svc,
    )
    assert gw.seen_calls[0]["alias"] == "fitt-smart"
    assert gw.seen_calls[0]["session_id"] == "main"
    assert gw.seen_calls[0]["messages"] == [{"role": "user", "content": "hi there"}]
    # The placeholder was sent, and at least one edit happened at
    # the end.
    assert bot.sent, "expected placeholder"
    assert bot.edits, "expected an edit with streamed content"


async def test_text_message_empty_text_skipped(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_text(bot, IncomingUpdate(user_id=42, chat_id=100, text="   "), svc)
    assert bot.sent == []


# ---------- photo -------------------------------------------------


async def test_photo_forwards_as_multimodal(tmp_path: Path) -> None:
    gw = FakeGateway(["this is", " an image"])
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_photo(
        bot,
        IncomingUpdate(
            user_id=42,
            chat_id=100,
            photo_bytes=b"\x89PNG\r\n\x1a\n\x00\x00",
            photo_mime="image/png",
            photo_caption="what is this?",
        ),
        svc,
    )
    call = gw.seen_calls[0]
    content = call["messages"][0]["content"]
    assert isinstance(content, list)
    types = [p.get("type") for p in content]
    assert "image_url" in types
    assert "text" in types
    # Caption was kept
    text_part = next(p for p in content if p["type"] == "text")
    assert text_part["text"] == "what is this?"


async def test_photo_without_caption_uses_default(tmp_path: Path) -> None:
    gw = FakeGateway(["ok"])
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_photo(
        bot,
        IncomingUpdate(
            user_id=42,
            chat_id=100,
            photo_bytes=b"X",
            photo_mime="image/jpeg",
            photo_caption=None,
        ),
        svc,
    )
    content = gw.seen_calls[0]["messages"][0]["content"]
    text_part = next(p for p in content if p["type"] == "text")
    assert "Describe" in text_part["text"]


# ---------- voice (stub) ------------------------------------------


async def test_voice_message_stub_reply(tmp_path: Path) -> None:
    bot = FakeBot()
    await handlers.handle_voice(bot, IncomingUpdate(user_id=42, chat_id=100, is_voice=True))
    assert len(bot.sent) == 1
    assert "not wired up" in bot.sent[0][1].lower()


# ---------- /session ----------------------------------------------


async def test_session_list_marks_current(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    svc.sessions.create("retroai", "Retro AI")
    bot = FakeBot()
    await handlers.handle_session_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="session", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "main" in text
    assert "retroai" in text
    assert "(current)" in text


async def test_session_switch_valid(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    svc.sessions.create("retroai")
    bot = FakeBot()
    await handlers.handle_session_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="session", command_args=["retroai"]),
        svc,
    )
    assert svc.prefs.get(100).session_id == "retroai"
    assert "Switched" in bot.sent[0][1]


async def test_session_switch_unknown(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_session_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="session", command_args=["bogus"]),
        svc,
    )
    # Prefs unchanged
    assert svc.prefs.get(100).session_id == "main"
    reply = bot.sent[0][1]
    assert "Unknown" in reply
    assert "main" in reply


async def test_session_new_creates_and_switches(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_session_command(
        bot,
        IncomingUpdate(
            user_id=42,
            chat_id=100,
            command="session",
            command_args=["new", "retroai", "Retro", "AI"],
        ),
        svc,
    )
    assert "retroai" in {s.id for s in svc.sessions.all()}
    assert svc.prefs.get(100).session_id == "retroai"
    # Custom display name preserved
    session = svc.sessions.get("retroai")
    assert session is not None
    assert session.name == "Retro AI"


async def test_session_new_invalid_id(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_session_command(
        bot,
        IncomingUpdate(
            user_id=42,
            chat_id=100,
            command="session",
            command_args=["new", "BAD-ID"],
        ),
        svc,
    )
    assert "BAD-ID" not in {s.id for s in svc.sessions.all()}
    assert "Invalid id" in bot.sent[0][1]


# ---------- /model ------------------------------------------------


async def test_model_list_marks_current(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_model_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="model", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "fitt-default" in text
    assert "(current)" in text


async def test_model_switch_valid(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_model_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="model", command_args=["fitt-smart"]),
        svc,
    )
    assert svc.prefs.get(100).alias == "fitt-smart"


async def test_model_switch_unknown(tmp_path: Path) -> None:
    svc = _services(tmp_path)
    bot = FakeBot()
    await handlers.handle_model_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="model", command_args=["nope"]),
        svc,
    )
    assert svc.prefs.get(100).alias == "fitt-default"
    assert "Unknown alias" in bot.sent[0][1]

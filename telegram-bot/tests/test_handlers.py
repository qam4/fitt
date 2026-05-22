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

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
    ):
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": 1000 + len(self.sent)})()

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> None:
        self.edits.append((chat_id, message_id, text))


class FakeGateway:
    def __init__(
        self,
        deltas: list[str],
        aliases: list[str] | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self._deltas = deltas
        self._aliases = aliases or ["fitt-default", "fitt-smart", "fitt-fast"]
        # Default: build details from the alias list with a stable
        # backend/model shape that matches the gateway's
        # /v1/models response. Tests that need the alias-only
        # fallback path can pass details=[].
        if details is None:
            details = [
                {
                    "id": a,
                    "object": "model",
                    "fitt_backend": "ollama" if "fast" in a else "openrouter",
                    "fitt_resolved_model": f"{a}-model",
                    "fitt_fallback": None,
                }
                for a in self._aliases
            ]
        self._details = details
        self._captures: list[dict[str, Any]] = []
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

    async def list_alias_details(self) -> list[dict[str, Any]]:
        return self._details

    async def list_recent_captures(
        self,
        session_id: str,
        *,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        # Return what the test injected via ``set_captures``.
        return list(self._captures[:limit])

    async def get_capture(
        self,
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        for cap in self._captures:
            if cap.get("turn_id") == turn_id:
                return cap
        return None

    def set_captures(self, captures: list[dict[str, Any]]) -> None:
        self._captures = captures


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


async def test_model_list_shows_concrete_model_and_backend(tmp_path: Path) -> None:
    """Phase 7 visibility: /model surfaces the concrete model and
    backend each alias resolves to. Closes the granite-style "I
    don't know what model just answered" gap without ssh'ing into
    the hub.
    """
    gw = FakeGateway(
        deltas=[],
        aliases=["fitt-default", "fitt-smart"],
        details=[
            {
                "id": "fitt-default",
                "object": "model",
                "fitt_backend": "ollama",
                "fitt_resolved_model": "granite3.3:8b",
                "fitt_fallback": None,
            },
            {
                "id": "fitt-smart",
                "object": "model",
                "fitt_backend": "openrouter",
                "fitt_resolved_model": "anthropic/claude-sonnet-4.5",
                "fitt_fallback": "fitt-default-fallback",
            },
        ],
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_model_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="model", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "fitt-default → granite3.3:8b (ollama)" in text
    assert "fitt-smart → anthropic/claude-sonnet-4.5 (openrouter)" in text
    assert "fallback: fitt-default-fallback" in text
    assert "(current)" in text  # marks current alias


async def test_model_list_falls_back_when_extensions_missing(tmp_path: Path) -> None:
    """Older gateway responses (no fitt_resolved_model /
    fitt_backend) shouldn't break the command — degrade to the
    alias-only display."""
    gw = FakeGateway(
        deltas=[],
        aliases=["fitt-default"],
        details=[{"id": "fitt-default", "object": "model"}],
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_model_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="model", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "fitt-default" in text
    # No "→" because the extensions weren't available.
    assert "→" not in text


async def test_model_switch_confirms_with_concrete_model(tmp_path: Path) -> None:
    """Switching alias should confirm with the concrete model
    so the user sees what they're now talking to without a
    follow-up /model call."""
    gw = FakeGateway(
        deltas=[],
        aliases=["fitt-default", "fitt-smart"],
        details=[
            {
                "id": "fitt-default",
                "fitt_backend": "ollama",
                "fitt_resolved_model": "granite3.3:8b",
            },
            {
                "id": "fitt-smart",
                "fitt_backend": "openrouter",
                "fitt_resolved_model": "anthropic/claude-sonnet-4.5",
            },
        ],
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_model_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="model", command_args=["fitt-smart"]),
        svc,
    )
    assert svc.prefs.get(100).alias == "fitt-smart"
    text = bot.sent[0][1]
    assert "anthropic/claude-sonnet-4.5" in text
    assert "openrouter" in text


# ---------- /lastturn ---------------------------------------------


async def test_lastturn_with_no_captures_says_so(tmp_path: Path) -> None:
    """A fresh chat with no captured turn yet returns a clear
    'no recent turn' message rather than a 404 / empty
    response."""
    gw = FakeGateway(deltas=[])
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "No recent captured turn" in text
    assert "main" in text  # default session


async def test_lastturn_renders_summary(tmp_path: Path) -> None:
    """The granite-style debugging case end-to-end: operator
    types /lastturn, sees the prompt-fill numbers that took
    two hours of curl-comparison to find."""
    gw = FakeGateway(deltas=[])
    gw.set_captures(
        [
            {
                "turn_id": "abc12345-6789-...",
                "session_key": "main",
                "alias": "fitt-default",
                "client": "telegram",
                "model_used": "granite3.3:8b",
                "backend": "ollama",
                "fallback_used": False,
                "started_at": 1779479823.42,
                "finished_at": 1779479825.81,
                "prompt_tokens": 5400,
                "completion_tokens": 89,
                "context_window": 32768,
                "prompt_pct_of_window": 16.5,
                "finish_reason": "stop",
                "narration_warning": False,
                "iterations": 1,
                "tool_calls_count": 0,
                "status": "ok",
            }
        ]
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    # Header + alias/model line.
    assert "abc12345" in text
    assert "fitt-default" in text
    assert "granite3.3:8b" in text
    assert "ollama" in text
    # Token + window + pct.
    assert "5,400" in text
    assert "32,768" in text
    assert "16.5%" in text
    assert "89" in text
    # Latency derived from started_at / finished_at; float math
    # gives 2389ms or 2390ms depending on Python version.
    assert "238" in text or "239" in text  # ms prefix
    assert " ms" in text
    # Finish reason.
    assert "stop" in text


async def test_lastturn_flags_high_context_usage(tmp_path: Path) -> None:
    """When prompt_pct_of_window is at or above 80%, the
    rendering surfaces a warning glyph so it's visible at a
    glance. Anchors the operator's eye on the variable that
    matters."""
    gw = FakeGateway(deltas=[])
    gw.set_captures(
        [
            {
                "turn_id": "high",
                "alias": "fitt-default",
                "model_used": "qwen2.5:14b",
                "backend": "ollama",
                "prompt_tokens": 26000,
                "completion_tokens": 100,
                "context_window": 32768,
                "prompt_pct_of_window": 79.3,  # under 80, no glyph
                "finish_reason": "stop",
                "status": "ok",
            }
        ]
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "79.3%" in text
    # No warning at 79.
    assert "⚠ <b>79.3%</b>" not in text

    # Bump above threshold.
    gw.set_captures(
        [
            {
                "turn_id": "high2",
                "alias": "fitt-default",
                "model_used": "qwen2.5:14b",
                "backend": "ollama",
                "prompt_tokens": 28000,
                "completion_tokens": 100,
                "context_window": 32768,
                "prompt_pct_of_window": 85.4,
                "finish_reason": "stop",
                "status": "ok",
            }
        ]
    )
    bot2 = FakeBot()
    await handlers.handle_lastturn_command(
        bot2,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    text2 = bot2.sent[0][1]
    assert "85.4%" in text2
    assert "⚠" in text2  # warning glyph present


async def test_lastturn_renders_narration_warning(tmp_path: Path) -> None:
    """A captured turn with the narration warning flag shows
    an explicit hint about the granite-style failure mode."""
    gw = FakeGateway(deltas=[])
    gw.set_captures(
        [
            {
                "turn_id": "warn-1",
                "alias": "fitt-default",
                "model_used": "granite3.3:8b",
                "backend": "ollama",
                "prompt_tokens": 5400,
                "completion_tokens": 100,
                "context_window": 32768,
                "prompt_pct_of_window": 16.5,
                "finish_reason": "stop",
                "narration_warning": True,
                "status": "ok",
            }
        ]
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "narration warning" in text
    assert "granite-style" in text


async def test_lastturn_handles_unknown_context_window(tmp_path: Path) -> None:
    """When discovery failed for the bound model, context_window
    is None — the rendering says 'window unknown' rather than
    showing 0 or a bogus pct."""
    gw = FakeGateway(deltas=[])
    gw.set_captures(
        [
            {
                "turn_id": "no-cw",
                "alias": "fitt-default",
                "model_used": "future/model",
                "backend": "anthropic",
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "context_window": None,
                "prompt_pct_of_window": None,
                "finish_reason": "stop",
                "status": "ok",
            }
        ]
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "1,000" in text
    assert "window unknown" in text
    assert "%" not in text or "completion" in text  # no spurious pct


async def test_lastturn_renders_failure_status(tmp_path: Path) -> None:
    """Non-ok statuses (upstream_error, tool_loop_exhausted)
    surface explicitly so the operator doesn't have to scan
    for the failure cause."""
    gw = FakeGateway(deltas=[])
    gw.set_captures(
        [
            {
                "turn_id": "fail-1",
                "alias": "fitt-default",
                "model_used": "granite3.3:8b",
                "backend": "ollama",
                "prompt_tokens": 5400,
                "completion_tokens": 0,
                "context_window": 32768,
                "prompt_pct_of_window": 16.5,
                "finish_reason": "stop",
                "iterations": 10,
                "status": "tool_loop_exhausted",
            }
        ]
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "tool_loop_exhausted" in text
    assert "⚠" in text


async def test_lastturn_uses_chat_session(tmp_path: Path) -> None:
    """The command reads the chat's preferred session, not the
    default 'main', so a per-chat /session switch is honoured."""
    gw = FakeGateway(deltas=[])
    seen: list[str] = []

    original = gw.list_recent_captures

    async def _track(session_id: str, *, limit: int = 1) -> list[dict[str, Any]]:
        seen.append(session_id)
        return await original(session_id, limit=limit)

    gw.list_recent_captures = _track  # type: ignore[method-assign]
    svc = _services(tmp_path, gateway=gw)
    svc.sessions.create("retroai", "Retro AI debugging")
    svc.prefs.set_session(100, "retroai")

    bot = FakeBot()
    await handlers.handle_lastturn_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="lastturn", command_args=[]),
        svc,
    )
    assert seen == ["retroai"]

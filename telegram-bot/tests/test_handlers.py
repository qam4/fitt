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
        self._status: dict[str, Any] | None = None
        self._eval_response: dict[str, Any] | None = None
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

    async def get_status(self) -> dict[str, Any] | None:
        return self._status

    async def run_eval(self, alias: str) -> dict[str, Any] | None:
        # Tests inject either an explicit summary, an error
        # envelope, or None (transport failure).
        return self._eval_response

    def set_captures(self, captures: list[dict[str, Any]]) -> None:
        self._captures = captures

    def set_status(self, status: dict[str, Any] | None) -> None:
        self._status = status

    def set_eval_response(self, response: dict[str, Any] | None) -> None:
        self._eval_response = response


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


# ---------- /status -----------------------------------------------


async def test_status_handles_gateway_unreachable(tmp_path: Path) -> None:
    """When the gateway client returns None (transport error,
    auth failure), the bot renders a clear error rather than
    crashing."""
    gw = FakeGateway(deltas=[])
    gw.set_status(None)
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_status_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="status", command_args=[]),
        svc,
    )
    assert len(bot.sent) == 1
    assert "Could not reach" in bot.sent[0][1]


async def test_status_renders_aggregate(tmp_path: Path) -> None:
    """A typical status response renders every section the
    operator looks for: uptime, mcp, cron, gaps, pruners,
    telegram."""
    gw = FakeGateway(deltas=[])
    import time as _time

    now = _time.time()
    gw.set_status(
        {
            "generated_at": now,
            "gateway": {"uptime_s": 3725.0, "started_at": now - 3725.0},
            "mcp": {"servers_total": 2, "servers_running": 2},
            "cron": {"total": 3, "enabled": 2, "next_firing": now + 300.0},
            "capability_gaps": {"total": 5},
            "pruners": {
                "history_last_sweep": now - 86400.0,  # one day ago
                "events_last_sweep": now - 3600.0,  # one hour ago
            },
            "telegram": {"configured": True},
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_status_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="status", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "FITT status" in text
    assert "uptime" in text
    # 3725s = 1h2m.
    assert "1h2m" in text
    assert "2/2 running" in text  # mcp
    assert "2/3 enabled" in text  # cron enabled / total
    # next_firing is ~300s out; rendering rounds down to "4m" or
    # "5m" depending on exactly how much wall clock elapsed
    # between set_status and the format call.
    assert ("next in 4m" in text) or ("next in 5m" in text)
    assert "5</code> recorded" in text  # gap count formatted
    assert "1d ago" in text  # history pruner
    assert ("1h ago" in text) or ("59m ago" in text)  # events pruner
    assert "configured" in text


async def test_status_warns_on_partial_mcp(tmp_path: Path) -> None:
    """One down MCP server out of two surfaces with a warning
    glyph so the operator notices."""
    gw = FakeGateway(deltas=[])
    gw.set_status(
        {
            "generated_at": 1779479823.0,
            "gateway": {"uptime_s": 60.0, "started_at": 1779479763.0},
            "mcp": {"servers_total": 2, "servers_running": 1},
            "cron": {"total": 0, "enabled": 0, "next_firing": None},
            "capability_gaps": {"total": 0},
            "pruners": {"history_last_sweep": None, "events_last_sweep": None},
            "telegram": {"configured": True},
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_status_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="status", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "1/2 running" in text
    assert "⚠" in text


async def test_status_handles_no_cron_jobs(tmp_path: Path) -> None:
    gw = FakeGateway(deltas=[])
    gw.set_status(
        {
            "gateway": {"uptime_s": 60.0, "started_at": 0},
            "mcp": {"servers_total": 0, "servers_running": 0},
            "cron": {"total": 0, "enabled": 0, "next_firing": None},
            "capability_gaps": {"total": 0},
            "pruners": {"history_last_sweep": None, "events_last_sweep": None},
            "telegram": {"configured": False},
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_status_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="status", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "no jobs" in text
    assert "none configured" in text  # mcp
    # Telegram unconfigured is also surfaced as a warning so an
    # operator setting up notices the gap.
    assert "not configured" in text


async def test_status_warns_when_gaps_exist(tmp_path: Path) -> None:
    """Capability gaps recorded → surface the count so the
    operator knows there's a backlog of "I'd need a tool"
    feedback."""
    gw = FakeGateway(deltas=[])
    gw.set_status(
        {
            "gateway": {"uptime_s": 60.0, "started_at": 0},
            "mcp": {"servers_total": 0, "servers_running": 0},
            "cron": {"total": 0, "enabled": 0, "next_firing": None},
            "capability_gaps": {"total": 7},
            "pruners": {"history_last_sweep": None, "events_last_sweep": None},
            "telegram": {"configured": True},
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_status_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="status", command_args=[]),
        svc,
    )
    text = bot.sent[0][1]
    assert "7" in text
    assert "recorded" in text


# ---------- /eval -------------------------------------------------


async def test_eval_posts_running_placeholder_then_edits(tmp_path: Path) -> None:
    """The bot replies with a "running…" placeholder
    immediately so the operator sees the command was
    accepted, then edits in the result when the gateway
    returns. Without the placeholder the chat looks frozen
    for 15-25 seconds."""
    gw = FakeGateway(deltas=[])
    gw.set_eval_response(
        {
            "alias": "fitt-default",
            "model_id": "qwen2.5-coder:14b",
            "started_at": "2026-05-22T19:00:00+00:00",
            "finished_at": "2026-05-22T19:00:30+00:00",
            "duration_ms": 30_000,
            "passed": 4,
            "failed": 1,
            "total": 5,
            "pass_rate": 0.8,
            "cases": [
                {
                    "name": "read_file_basic",
                    "status": "pass",
                    "detail": "called read_file",
                    "latency_ms": 120,
                    "tool_called": "read_file",
                    "finish_reason": "tool_calls",
                },
                {
                    "name": "narrated_case",
                    "status": "narrated",
                    "detail": "model replied with text instead of tool_calls",
                    "latency_ms": 1500,
                    "tool_called": None,
                    "finish_reason": "stop",
                },
            ],
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_eval_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="eval", command_args=[]),
        svc,
    )
    # One send (the placeholder) + one edit (the result).
    assert len(bot.sent) == 1
    assert "Running eval suite" in bot.sent[0][1]
    assert "fitt-default" in bot.sent[0][1]  # default chat alias
    assert len(bot.edits) == 1
    edited = bot.edits[0][2]
    assert "4/5</b> passed" in edited or "4/5" in edited
    assert "80%" in edited
    assert "qwen2.5-coder:14b" in edited
    # Per-case rendering: pass + narrated.
    assert "read_file_basic" in edited
    assert "narrated_case" in edited
    assert "✅" in edited
    assert "❌" in edited


async def test_eval_uses_explicit_alias_arg(tmp_path: Path) -> None:
    """``/eval fitt-smart`` overrides the chat's default alias."""
    gw = FakeGateway(deltas=[])
    gw.set_eval_response(
        {
            "alias": "fitt-smart",
            "model_id": "claude-sonnet-4.5",
            "started_at": "2026-05-22T19:00:00+00:00",
            "finished_at": "2026-05-22T19:00:30+00:00",
            "duration_ms": 30_000,
            "passed": 5,
            "failed": 0,
            "total": 5,
            "pass_rate": 1.0,
            "cases": [],
        }
    )
    seen_aliases: list[str] = []

    original_run_eval = gw.run_eval

    async def _track(alias: str) -> dict[str, Any] | None:
        seen_aliases.append(alias)
        return await original_run_eval(alias)

    gw.run_eval = _track  # type: ignore[method-assign]
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_eval_command(
        bot,
        IncomingUpdate(
            user_id=42,
            chat_id=100,
            command="eval",
            command_args=["fitt-smart"],
        ),
        svc,
    )
    assert seen_aliases == ["fitt-smart"]
    assert "fitt-smart" in bot.sent[0][1]


async def test_eval_handles_unknown_alias_error(tmp_path: Path) -> None:
    gw = FakeGateway(deltas=[])
    gw.set_eval_response(
        {
            "error": {
                "type": "unknown_alias",
                "message": "alias 'nope' not configured",
                "available": ["fitt-default", "fitt-smart"],
            }
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_eval_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="eval", command_args=["nope"]),
        svc,
    )
    edited = bot.edits[-1][2]
    assert "Unknown alias" in edited
    assert "nope" in edited
    assert "fitt-default" in edited  # available list rendered


async def test_eval_handles_infrastructure_failure(tmp_path: Path) -> None:
    gw = FakeGateway(deltas=[])
    gw.set_eval_response(
        {
            "error": {
                "type": "eval_infrastructure_failure",
                "message": "harness broke",
            }
        }
    )
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_eval_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="eval", command_args=[]),
        svc,
    )
    edited = bot.edits[-1][2]
    assert "Eval failed" in edited
    assert "eval_infrastructure_failure" in edited


async def test_eval_handles_gateway_unreachable(tmp_path: Path) -> None:
    gw = FakeGateway(deltas=[])
    gw.set_eval_response(None)  # transport failure
    svc = _services(tmp_path, gateway=gw)
    bot = FakeBot()
    await handlers.handle_eval_command(
        bot,
        IncomingUpdate(user_id=42, chat_id=100, command="eval", command_args=[]),
        svc,
    )
    edited = bot.edits[-1][2]
    assert "gateway unreachable" in edited

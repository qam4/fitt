"""Telegram message and command handlers.

The handlers are split in two layers:

* Pure business functions (``handle_text``, ``handle_photo``, ...)
  that take a tiny ``Services`` bag and work with already-parsed
  data. These are unit-testable without any python-telegram-bot
  plumbing.
* Thin ``python-telegram-bot``-style wrappers in ``bot.py`` that
  extract what they need from ``Update`` / ``CallbackContext`` and
  call into the pure layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from gateway.sessions import (
    DuplicateSessionId,
    InvalidSessionId,
    SessionRegistry,
)

from .gateway_client import GatewayClient
from .prefs import ChatPrefs, PrefsStore
from .streaming import StreamingEditor

_log = logging.getLogger(__name__)


# ---------- dependency bag ----------------------------------------


@dataclass
class Services:
    """Everything a handler might need. Handed to each call."""

    gateway: GatewayClient
    prefs: PrefsStore
    sessions: SessionRegistry
    allowlist: frozenset[int]


# ---------- structural types --------------------------------------


class SenderBot(Protocol):
    """What we need from python-telegram-bot's ``Bot`` object."""

    async def send_message(
        self, *, chat_id: int, text: str, parse_mode: str | None = None
    ) -> SentMessage: ...
    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
    ) -> object: ...


class SentMessage(Protocol):
    message_id: int


@dataclass
class IncomingUpdate:
    """Normalised slice of a Telegram ``Update`` for the pure
    handlers. The bot.py wrapper is responsible for extracting
    these fields from a real Update object."""

    user_id: int
    chat_id: int
    text: str | None = None
    command: str | None = None
    command_args: list[str] = field(default_factory=list)
    photo_bytes: bytes | None = None
    photo_mime: str | None = None
    photo_caption: str | None = None
    is_voice: bool = False


# ---------- allowlist ---------------------------------------------


def is_allowed(services: Services, user_id: int) -> bool:
    return user_id in services.allowlist


async def drop_unauthorised(update: IncomingUpdate) -> None:
    _log.info(
        "telegram.rejected_non_allowlisted",
        extra={"user_id": update.user_id, "chat_id": update.chat_id},
    )


# ---------- text message ------------------------------------------


async def handle_text(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    """Forward plain-text message to the gateway, stream reply
    back as an edit-in-place message."""
    prefs = services.prefs.get(update.chat_id)
    text = update.text or ""
    if not text.strip():
        return

    placeholder = await bot.send_message(chat_id=update.chat_id, text="…")
    editor = StreamingEditor(
        bot=bot,  # type: ignore[arg-type]
        chat_id=update.chat_id,
        message_id=placeholder.message_id,
    )

    messages = [{"role": "user", "content": text}]
    async for delta in services.gateway.chat(
        messages=messages,
        alias=prefs.alias,
        session_id=prefs.session_id,
    ):
        await editor.append(delta)
    await editor.finalize()


# ---------- photo message -----------------------------------------


async def handle_photo(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    """Forward a photo + caption to the gateway as a multimodal
    user message."""
    if update.photo_bytes is None:
        return
    prefs = services.prefs.get(update.chat_id)

    import base64

    b64 = base64.b64encode(update.photo_bytes).decode("ascii")
    mime = update.photo_mime or "image/jpeg"
    data_url = f"data:{mime};base64,{b64}"

    content: list[dict[str, object]] = [
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    caption = update.photo_caption or "Describe this image."
    content.append({"type": "text", "text": caption})

    placeholder = await bot.send_message(chat_id=update.chat_id, text="…")
    editor = StreamingEditor(
        bot=bot,  # type: ignore[arg-type]
        chat_id=update.chat_id,
        message_id=placeholder.message_id,
    )
    messages = [{"role": "user", "content": content}]
    async for delta in services.gateway.chat(
        messages=messages,
        alias=prefs.alias,
        session_id=prefs.session_id,
    ):
        await editor.append(delta)
    await editor.finalize()


# ---------- voice message (Phase 3 stub) --------------------------


async def handle_voice(bot: SenderBot, update: IncomingUpdate) -> None:
    await bot.send_message(
        chat_id=update.chat_id,
        text=("Voice is not wired up yet. Phase 8 adds STT/TTS. Send a text message for now."),
    )


# ---------- /session ----------------------------------------------


async def handle_session_command(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    args = update.command_args
    current = services.prefs.get(update.chat_id)

    if not args:
        await _reply_session_list(bot, update.chat_id, services, current)
        return

    if args[0] == "new":
        if len(args) < 2:
            await bot.send_message(
                chat_id=update.chat_id,
                text="Usage: /session new <id> [<display name>]",
            )
            return
        sid = args[1]
        name = " ".join(args[2:]) or None
        try:
            s = services.sessions.create(sid, name)
        except InvalidSessionId as e:
            await bot.send_message(chat_id=update.chat_id, text=f"Invalid id: {e}")
            return
        except DuplicateSessionId as e:
            await bot.send_message(chat_id=update.chat_id, text=str(e))
            return
        services.prefs.set_session(update.chat_id, s.id)
        await bot.send_message(
            chat_id=update.chat_id,
            text=f"Created session '{s.id}' (name: {s.name}). Switched.",
        )
        return

    # Plain /session <id>: switch
    sid = args[0]
    valid = services.sessions.valid_ids()
    if sid not in valid:
        await bot.send_message(
            chat_id=update.chat_id,
            text=(
                f"Unknown session '{sid}'. Active: {', '.join(sorted(valid))}."
                " Use /session new <id> to create a new one."
            ),
        )
        return
    services.prefs.set_session(update.chat_id, sid)
    await bot.send_message(chat_id=update.chat_id, text=f"Switched this chat to session '{sid}'.")


async def _reply_session_list(
    bot: SenderBot,
    chat_id: int,
    services: Services,
    current: ChatPrefs,
) -> None:
    sessions = services.sessions.all()
    if not sessions:
        # Shouldn't happen (main is always present) but be defensive.
        await bot.send_message(chat_id=chat_id, text="No sessions configured.")
        return
    lines = ["*Sessions:*"]
    for s in sessions:
        marker = " (current)" if s.id == current.session_id else ""
        lines.append(f"- {s.id}: {s.name}{marker}")
    lines.append("")
    lines.append("Switch with /session <id>, create with /session new <id>.")
    await bot.send_message(chat_id=chat_id, text="\n".join(lines))


# ---------- /model ------------------------------------------------


async def handle_model_command(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    current = services.prefs.get(update.chat_id)
    details = await services.gateway.list_alias_details()
    if not details:
        await bot.send_message(
            chat_id=update.chat_id,
            text="Could not fetch aliases from the gateway.",
        )
        return

    aliases = [d["id"] for d in details if "id" in d]

    if not update.command_args:
        # No arg: list aliases with concrete model + backend so the
        # operator can see what each alias actually resolves to.
        # Phase 7 visibility work — closes the "I don't know which
        # model just answered" gap. The gateway already exposes
        # ``fitt_resolved_model`` and ``fitt_backend`` per alias on
        # ``/v1/models``; we just hadn't been rendering them.
        lines = ["*Aliases:*"]
        for d in details:
            alias_id = d.get("id", "?")
            model = d.get("fitt_resolved_model")
            backend = d.get("fitt_backend")
            fallback = d.get("fitt_fallback")
            marker = " (current)" if alias_id == current.alias else ""
            if model and backend:
                line = f"- {alias_id} → {model} ({backend}){marker}"
            else:
                # Older gateways that don't surface the extensions:
                # degrade to the alias-only display rather than
                # erroring out.
                line = f"- {alias_id}{marker}"
            if fallback:
                line += f"\n  fallback: {fallback}"
            lines.append(line)
        lines.append("")
        lines.append("Switch with /model <alias>.")
        await bot.send_message(chat_id=update.chat_id, text="\n".join(lines))
        return

    target = update.command_args[0]
    if target not in aliases:
        await bot.send_message(
            chat_id=update.chat_id,
            text=f"Unknown alias '{target}'. Available: {', '.join(aliases)}.",
        )
        return
    services.prefs.set_alias(update.chat_id, target)
    # Confirm the switch with the concrete model the new alias
    # resolves to, so the user sees what they're now talking to.
    target_detail = next((d for d in details if d.get("id") == target), {})
    target_model = target_detail.get("fitt_resolved_model")
    target_backend = target_detail.get("fitt_backend")
    if target_model and target_backend:
        text = f"Switched this chat to alias '{target}' → {target_model} ({target_backend})."
    else:
        text = f"Switched this chat to alias '{target}'."
    await bot.send_message(chat_id=update.chat_id, text=text)


# ---------- /start, /help -----------------------------------------


async def handle_start(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    prefs = services.prefs.get(update.chat_id)
    await bot.send_message(
        chat_id=update.chat_id,
        text=(
            "Hi. I'm FITT. I forward your messages to your personal "
            "gateway at home.\n"
            f"\nCurrent alias: {prefs.alias}\n"
            f"Current session: {prefs.session_id}\n"
            "\n/help for commands."
        ),
    )


async def handle_help(bot: SenderBot, update: IncomingUpdate) -> None:
    await bot.send_message(
        chat_id=update.chat_id,
        text=(
            "Commands:\n"
            "/start - show status\n"
            "/session - list sessions\n"
            "/session <id> - switch session\n"
            "/session new <id> [name] - create a new session\n"
            "/model - list aliases\n"
            "/model <alias> - switch alias\n"
            "/lastturn - show detail for the most recent turn\n"
            "/status - system snapshot (mcp, cron, gaps, uptime)\n"
            "/help - this message\n"
            "\nPhoto messages get multimodal analysis. Voice is not "
            "wired up yet."
        ),
    )


# ---------- /status -----------------------------------------------


async def handle_status_command(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    """Phase 7 Slice 7.3: operator-facing system snapshot.

    Aggregates uptime, MCP server up/down, cron job count + next
    firing, capability-gap log size, pruner cadences, and
    Telegram-configured flag. The "is FITT okay right now?"
    answer at a glance — without ssh into the hub."""
    status = await services.gateway.get_status()
    if status is None:
        await bot.send_message(
            chat_id=update.chat_id,
            text="Could not reach the gateway for status.",
        )
        return
    text = _format_status(status)
    await bot.send_message(chat_id=update.chat_id, text=text, parse_mode="HTML")


def _format_status(status: dict[str, Any]) -> str:
    """Render the /v1/status payload as Telegram HTML."""
    import html as _html

    lines: list[str] = ["<b>FITT status</b>"]

    # Gateway uptime.
    gw = status.get("gateway") or {}
    uptime_s = gw.get("uptime_s")
    if isinstance(uptime_s, (int, float)):
        lines.append(f"uptime <code>{_html.escape(_format_duration(uptime_s))}</code>")

    # MCP servers.
    mcp = status.get("mcp") or {}
    total = int(mcp.get("servers_total") or 0)
    running = int(mcp.get("servers_running") or 0)
    if total == 0:
        lines.append("mcp servers: <code>none configured</code>")
    elif running == total:
        lines.append(f"mcp servers: <code>{running}/{total} running</code>")
    else:
        # Partial running is a warning condition.
        lines.append(f"⚠ mcp servers: <code>{running}/{total} running</code>")

    # Cron.
    cron = status.get("cron") or {}
    cron_total = int(cron.get("total") or 0)
    cron_enabled = int(cron.get("enabled") or 0)
    next_firing = cron.get("next_firing")
    if cron_total == 0:
        lines.append("cron: <code>no jobs</code>")
    else:
        cron_line = f"cron: <code>{cron_enabled}/{cron_total} enabled</code>"
        if isinstance(next_firing, (int, float)):
            cron_line += f", next {_format_until(next_firing)}"
        lines.append(cron_line)

    # Capability gaps.
    gaps = status.get("capability_gaps") or {}
    gap_total = int(gaps.get("total") or 0)
    if gap_total > 0:
        lines.append(f"capability gaps: <code>{gap_total}</code> recorded")

    # Pruner cadences.
    pruners = status.get("pruners") or {}
    history_last = pruners.get("history_last_sweep")
    events_last = pruners.get("events_last_sweep")
    if isinstance(history_last, (int, float)):
        lines.append(f"history pruner: last swept {_format_ago(history_last)}")
    if isinstance(events_last, (int, float)):
        lines.append(f"events pruner: last swept {_format_ago(events_last)}")

    # Telegram presence.
    telegram = status.get("telegram") or {}
    if telegram.get("configured"):
        lines.append("telegram: <code>configured</code>")
    else:
        lines.append("⚠ telegram: <code>not configured</code>")

    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    """Compact duration: 1d2h, 3h45m, 12m, 45s. The status
    surface is at-a-glance — full ISO-8601 strings would be
    operator overhead."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, rem = divmod(s, 3600)
        m = rem // 60
        return f"{h}h{m}m" if m else f"{h}h"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d{h}h" if h else f"{d}d"


def _format_until(future_ts: float) -> str:
    """Compact "in N units" rendering. Negative results (the
    timestamp's already past) say "now" rather than producing
    a misleading "-3m"."""
    import time as _time

    delta = future_ts - _time.time()
    if delta < 0:
        return "now"
    return f"in {_format_duration(delta)}"


def _format_ago(past_ts: float) -> str:
    """Compact "N units ago"."""
    import time as _time

    delta = _time.time() - past_ts
    if delta < 0:
        return "in the future"
    return f"{_format_duration(delta)} ago"


# ---------- /lastturn ---------------------------------------------


async def handle_lastturn_command(
    bot: SenderBot,
    update: IncomingUpdate,
    services: Services,
) -> None:
    """Phase 7 Slice 7.3: surface per-turn detail without
    leaving Telegram.

    Closes the granite-style debugging loop: the operator hits
    a surprising reply, types ``/lastturn``, sees what the
    model actually saw — alias / model / prompt size /
    context-window fill / finish reason / tool calls / latency.
    A 30-second answer to "what just happened" instead of an
    ssh-into-container plus six-tab grep.

    Reads the most recent capture for the chat's session via
    /v1/sessions/<s>/captures?limit=1. If capture was off for
    the originating client (router-mode / coding-agent), no
    capture exists; we say so."""
    prefs = services.prefs.get(update.chat_id)
    captures = await services.gateway.list_recent_captures(
        prefs.session_id,
        limit=1,
    )
    if not captures:
        await bot.send_message(
            chat_id=update.chat_id,
            text=(
                f"No recent captured turn for session '{prefs.session_id}'.\n"
                "\n"
                "Either no turn has run yet in this session, capture is "
                "disabled (traceability config), or the originating client "
                "was a coding-agent (which doesn't capture by default)."
            ),
        )
        return
    cap = captures[0]
    text = _format_lastturn(cap)
    await bot.send_message(chat_id=update.chat_id, text=text, parse_mode="HTML")


def _format_lastturn(cap: dict[str, Any]) -> str:
    """Render a captured turn summary as Telegram HTML.

    Layout is phone-friendly: short lines, key metrics
    grouped, warnings flagged, the granite case (5400 prompt
    tokens, narration warning) is read at a glance.
    """
    import html as _html

    turn_id = str(cap.get("turn_id", "?"))
    short_id = turn_id[:12] if len(turn_id) > 12 else turn_id
    alias = cap.get("alias", "?")
    model = cap.get("model_used", "?")
    backend = cap.get("backend", "?")
    prompt = int(cap.get("prompt_tokens") or 0)
    completion = int(cap.get("completion_tokens") or 0)
    window = cap.get("context_window")
    pct = cap.get("prompt_pct_of_window")
    finish_reason = cap.get("finish_reason") or "?"
    fallback = bool(cap.get("fallback_used"))
    narration_warning = bool(cap.get("narration_warning"))
    iterations = int(cap.get("iterations") or 0)
    tool_calls_count = int(cap.get("tool_calls_count") or 0)
    status = cap.get("status", "ok")

    # Latency: derived from started_at / finished_at.
    latency_ms: int | None = None
    started_at = cap.get("started_at")
    finished_at = cap.get("finished_at")
    if isinstance(started_at, (int, float)) and isinstance(finished_at, (int, float)):
        latency_ms = int((finished_at - started_at) * 1000)

    lines: list[str] = []
    lines.append(f"<b>Turn {_html.escape(short_id)}</b>")
    lines.append(
        f"alias <code>{_html.escape(str(alias))}</code> → "
        f"<code>{_html.escape(str(model))}</code> "
        f"({_html.escape(str(backend))})"
    )

    # Context fill — the granite case's load-bearing line.
    if window:
        pct_str = f"{pct:.1f}%" if isinstance(pct, (int, float)) else "?"
        if isinstance(pct, (int, float)) and pct >= 80:
            pct_str = f"⚠ <b>{pct_str}</b>"
        lines.append(f"prompt {prompt:,} / window {window:,} ({pct_str})")
    else:
        lines.append(f"prompt {prompt:,} (window unknown)")

    lines.append(f"completion {completion:,} tokens")
    if latency_ms is not None:
        lines.append(f"latency {latency_ms} ms")

    # Status + finish_reason — when status != ok the operator
    # wants to see why immediately.
    if status == "ok":
        lines.append(f"finish_reason <code>{_html.escape(str(finish_reason))}</code>")
    else:
        lines.append(f"⚠ status <code>{_html.escape(str(status))}</code>")

    if fallback:
        lines.append("(i) fallback used (primary backend failed)")

    if narration_warning:
        lines.append(
            "⚠ narration warning: shape suggests the model narrated a tool "
            "call rather than emitting one (granite-style; check the "
            "dispatched prompt size and the bound model)"
        )

    if iterations or tool_calls_count:
        lines.append(f"iterations {iterations}, tool calls {tool_calls_count}")

    return "\n".join(lines)

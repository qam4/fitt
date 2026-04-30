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
from typing import Protocol

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

    async def send_message(self, *, chat_id: int, text: str) -> SentMessage: ...
    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> object: ...


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
    aliases = await services.gateway.list_aliases()
    if not aliases:
        await bot.send_message(
            chat_id=update.chat_id,
            text="Could not fetch aliases from the gateway.",
        )
        return

    if not update.command_args:
        lines = ["*Aliases:*"]
        for a in aliases:
            marker = " (current)" if a == current.alias else ""
            lines.append(f"- {a}{marker}")
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
    await bot.send_message(
        chat_id=update.chat_id,
        text=f"Switched this chat to alias '{target}'.",
    )


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
            "/help - this message\n"
            "\nPhoto messages get multimodal analysis. Voice is not "
            "wired up yet."
        ),
    )

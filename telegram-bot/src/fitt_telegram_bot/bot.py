"""Bot application lifecycle.

Builds a ``python-telegram-bot`` ``Application``, wires up the
handlers declared in ``handlers.py``, and runs polling.

All python-telegram-bot imports are in this file so the handler
module can be unit-tested without pulling in the full PTB graph.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from gateway.sessions import SessionRegistry
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import handlers
from .config import TelegramBotConfig
from .gateway_client import GatewayClient
from .handlers import IncomingUpdate, Services
from .prefs import PrefsStore

_log = logging.getLogger(__name__)

_PureHandler = Callable[[Any, IncomingUpdate, Services], Awaitable[None]]


def build_application(bot_config: TelegramBotConfig) -> Application:
    """Construct a python-telegram-bot Application with FITT's
    handlers wired in."""

    prefs = PrefsStore(bot_config.prefs_path)
    sessions = SessionRegistry(bot_config.sessions_dir)
    sessions.ensure_main()
    gateway = GatewayClient(bot_config.gateway_url, bot_config.bearer_token)

    services = Services(
        gateway=gateway,
        prefs=prefs,
        sessions=sessions,
        allowlist=bot_config.allowlist_user_ids,
    )

    app = ApplicationBuilder().token(bot_config.bot_token).build()
    app.bot_data["services"] = services

    app.add_handler(CommandHandler("start", _wrap_command(_on_start)))
    app.add_handler(CommandHandler("help", _wrap_command(_on_help)))
    app.add_handler(CommandHandler("session", _wrap_command(_on_session)))
    app.add_handler(CommandHandler("model", _wrap_command(_on_model)))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, _on_photo_ptb))
    app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, _wrap_command(_on_voice)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _wrap_command(_on_text)))

    return app


# ---------- wrappers ---------------------------------------------


def _wrap_command(fn: _PureHandler):  # type: ignore[no-untyped-def]
    """Wrap a pure handler so PTB can call it with (update, context)."""

    async def _inner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        services = cast(Services, context.bot_data["services"])
        if update.effective_user is None or update.effective_chat is None:
            return
        if not handlers.is_allowed(services, update.effective_user.id):
            await handlers.drop_unauthorised(
                IncomingUpdate(
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                )
            )
            return
        parsed = _normalise(update)
        if parsed is None:
            return
        await fn(context.bot, parsed, services)

    return _inner


async def _on_photo_ptb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photo-specific wrapper: downloads the highest-resolution
    photo bytes before dispatching to the pure handler."""
    services = cast(Services, context.bot_data["services"])
    if update.effective_user is None or update.effective_chat is None:
        return
    if not handlers.is_allowed(services, update.effective_user.id):
        await handlers.drop_unauthorised(
            IncomingUpdate(
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
            )
        )
        return

    msg = update.effective_message
    if msg is None or not msg.photo:
        return

    # Telegram delivers the same photo in multiple resolutions; pick
    # the largest for best OCR/description quality.
    photo_file = await msg.photo[-1].get_file()
    photo_bytes = bytes(await photo_file.download_as_bytearray())

    parsed = IncomingUpdate(
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        photo_bytes=photo_bytes,
        photo_mime="image/jpeg",
        photo_caption=msg.caption,
    )
    await handlers.handle_photo(context.bot, parsed, services)


def _normalise(update: Update) -> IncomingUpdate | None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if user is None or chat is None:
        return None

    command: str | None = None
    args: list[str] = []
    if msg is not None and msg.text and msg.text.startswith("/"):
        parts = msg.text.split()
        command = parts[0][1:]
        args = parts[1:]

    text = msg.text if (msg and msg.text and not msg.text.startswith("/")) else None
    is_voice = bool(msg and msg.voice)

    return IncomingUpdate(
        user_id=user.id,
        chat_id=chat.id,
        text=text,
        command=command,
        command_args=args,
        is_voice=is_voice,
    )


# ---------- per-kind dispatch ------------------------------------


async def _on_start(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_start(bot, update, services)


async def _on_help(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_help(bot, update)


async def _on_session(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_session_command(bot, update, services)


async def _on_model(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_model_command(bot, update, services)


async def _on_text(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_text(bot, update, services)


async def _on_voice(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_voice(bot, update)

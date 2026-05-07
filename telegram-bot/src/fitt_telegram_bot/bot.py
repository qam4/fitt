"""Bot application lifecycle.

Builds a ``python-telegram-bot`` ``Application``, wires up the
handlers declared in ``handlers.py``, and runs polling.

All python-telegram-bot imports are in this file so the handler
module can be unit-tested without pulling in the full PTB graph.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from gateway.sessions import SessionRegistry
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import handlers
from .approval import (
    ApprovalPoller,
    build_callback_data,
    format_prompt,
    parse_callback_data,
)
from .config import TelegramBotConfig
from .gateway_client import GatewayClient
from .handlers import IncomingUpdate, Services
from .prefs import PrefsStore

_log = logging.getLogger(__name__)

_PureHandler = Callable[[Any, IncomingUpdate, Services], Awaitable[None]]


def build_application(bot_config: TelegramBotConfig) -> Application[Any, Any, Any, Any, Any, Any]:
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

    app = (
        ApplicationBuilder()
        .token(bot_config.bot_token)
        # Allow PTB to dispatch multiple updates in parallel on the
        # same event loop. Without this, updates are processed
        # strictly serially: a chat text handler that's awaiting a
        # long gateway reply blocks ALL other updates — including
        # the inline-keyboard callback that resolves the approval
        # that's holding the chat reply. That deadlocks until the
        # gateway's 45-second approval timeout fires, at which
        # point the chat returns with a tool error, the callback
        # finally runs, and its decide POST hits a 404 because the
        # approval was already cleaned up. See 2026-05 debug
        # session: every ask-bucket tool (git_commit, cron_add)
        # failed this way; spec_list worked because it was auto.
        #
        # A small bound rather than unlimited: single-user bot,
        # realistic peak is chat + approval-tap + a couple of
        # queued events. 4 leaves headroom without inviting
        # unbounded fan-out on a misbehaving gateway.
        .concurrent_updates(4)
        .build()
    )
    app.bot_data["services"] = services

    app.add_handler(CommandHandler("start", _wrap_command(_on_start)))
    app.add_handler(CommandHandler("help", _wrap_command(_on_help)))
    app.add_handler(CommandHandler("session", _wrap_command(_on_session)))
    app.add_handler(CommandHandler("model", _wrap_command(_on_model)))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, _on_photo_ptb))
    app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, _wrap_command(_on_voice)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _wrap_command(_on_text)))

    # Approval UI: inline-keyboard callback handler + background
    # poller. The poller is started via post_init so it runs
    # alongside update-processing on the same event loop. The
    # post_init/post_shutdown assignments are type-ignored because
    # PTB's stubs type these as a concrete (overly-parameterised)
    # Application type; we hand in generic Any-parameterised
    # callables so our code works against any Application shape.
    app.add_handler(CallbackQueryHandler(_on_approval_callback))
    app.post_init = _make_post_init(bot_config, services)  # type: ignore[assignment]
    app.post_shutdown = _on_post_shutdown

    return app


# ---------- approval poller lifecycle ----------------------------


def _make_post_init(
    bot_config: TelegramBotConfig, services: Services
) -> Callable[[Application[Any, Any, Any, Any, Any, Any]], Awaitable[None]]:
    """Returns a PTB `post_init` coroutine that starts the
    approval poller as a background task."""

    async def _post_init(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
        poller = ApprovalPoller(
            gateway=services.gateway,
            allowlist=bot_config.allowlist_user_ids,
            on_prompt=_make_on_prompt(app),
        )
        task = asyncio.create_task(poller.run(), name="approval-poller")
        app.bot_data["approval_poller"] = poller
        app.bot_data["approval_poller_task"] = task
        _log.info("telegram.approval_poller.scheduled")

    return _post_init


async def _on_post_shutdown(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Stop the poller cleanly on bot shutdown."""
    poller = app.bot_data.get("approval_poller")
    if poller is not None:
        poller.stop()
    task = app.bot_data.get("approval_poller_task")
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()


def _make_on_prompt(
    app: Application[Any, Any, Any, Any, Any, Any],
) -> Callable[[int, dict[str, Any]], Awaitable[None]]:
    """Return an ``on_prompt`` callback bound to this PTB app's
    bot handle. The poller uses this to post approval messages
    to allowlisted users."""

    async def _on_prompt(user_id: int, entry: dict[str, Any]) -> None:
        ap_id = entry.get("id", "")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve", callback_data=build_callback_data("approve", ap_id)
                    ),
                    InlineKeyboardButton(
                        "❌ Reject", callback_data=build_callback_data("reject", ap_id)
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔓 Trust session",
                        callback_data=build_callback_data("trust_session", ap_id),
                    )
                ],
            ]
        )
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text=format_prompt(entry),
                reply_markup=keyboard,
            )
        except Exception as e:
            _log.warning(
                "telegram.approval_prompt_failed",
                extra={"user_id": user_id, "approval_id": ap_id, "error": str(e)},
            )

    return _on_prompt


async def _on_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle an inline-keyboard button press on an approval prompt.

    Validates the callback payload, enforces the allowlist, posts
    the decision to the gateway, edits the original message to
    show the outcome.
    """
    query = update.callback_query
    if query is None or query.data is None:
        return

    services = cast(Services, context.bot_data["services"])
    user = update.effective_user
    if user is None or user.id not in services.allowlist:
        # Silently acknowledge to dismiss the spinner; don't
        # give any signal about what the callback is for.
        await query.answer()
        return

    try:
        decision, approval_id = parse_callback_data(query.data)
    except ValueError as e:
        _log.warning("telegram.approval_callback.malformed", extra={"error": str(e)})
        await query.answer("Invalid callback data.", show_alert=False)
        return

    ok, detail = await services.gateway.decide_approval(approval_id, decision)
    await query.answer()  # dismiss the spinner

    # Edit the original message so the user sees the outcome.
    outcome = f"✅ {decision.replace('_', ' ')}" if ok else f"⚠️ Failed: {detail or 'unknown error'}"
    msg = query.message
    if isinstance(msg, Message):
        try:
            original_text = msg.text or ""
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text=f"{original_text}\n\n{outcome}",
            )
        except Exception as e:
            # The message may already have been edited (double-tap);
            # don't let the UX hiccup propagate as an error.
            _log.debug(
                "telegram.approval_edit_failed",
                extra={"error": str(e), "approval_id": approval_id},
            )


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

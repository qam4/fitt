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
from .event_pusher import EventPusher
from .gateway_client import GatewayClient
from .handlers import IncomingUpdate, Services
from .prefs import PrefsStore
from .turn_renderer import TurnRenderer
from .turn_stream import TurnStreamMultiplexer

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
    app.add_handler(CommandHandler("lastturn", _wrap_command(_on_lastturn)))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, _on_photo_ptb))
    app.add_handler(MessageHandler(filters.VOICE & ~filters.COMMAND, _wrap_command(_on_voice)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text_ptb))

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
    approval poller + event pusher as background tasks."""

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

        # Phase 4.5 Task 7: event push subscriber. Polls the
        # gateway's event log and delivers new entries
        # (cron_fired, cron_completed, agent_message, late_tool_*)
        # to the allowlisted users.
        #
        # Cursor sits alongside prefs under $FITT_HOME/telegram/
        # so "everything the bot keeps on disk" lives in one
        # place.
        cursor_path = bot_config.prefs_path.parent / "pusher_cursor.json"
        pusher = EventPusher(
            gateway=services.gateway,
            allowlist=bot_config.allowlist_user_ids,
            on_event=_make_on_event(app),
            cursor_path=cursor_path,
        )
        pusher_task = asyncio.create_task(pusher.run(), name="event-pusher")
        app.bot_data["event_pusher"] = pusher
        app.bot_data["event_pusher_task"] = pusher_task
        _log.info("telegram.event_pusher.scheduled")

        # Phase 4.8b: per-turn SSE subscriber. Opens live
        # connections to the gateway's
        # ``/v1/sessions/<id>/turns/stream`` endpoint and
        # drives the growing-bubble + approval-bubble +
        # finish-footer Telegram UX via per-turn
        # :class:`TurnRenderer` instances.
        mux = TurnStreamMultiplexer(
            base_url=bot_config.gateway_url,
            bearer_token=bot_config.bearer_token,
            renderer_factory=_make_renderer_factory(app),
        )
        app.bot_data["turn_stream_mux"] = mux
        _log.info("telegram.turn_stream.ready")

    return _post_init


async def _on_post_shutdown(app: Application[Any, Any, Any, Any, Any, Any]) -> None:
    """Stop both pollers cleanly on bot shutdown."""
    for key in ("approval_poller", "event_pusher"):
        obj = app.bot_data.get(key)
        if obj is not None:
            obj.stop()
    for key in ("approval_poller_task", "event_pusher_task"):
        task = app.bot_data.get(key)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
    # Phase 4.8b: stop the turn-stream multiplexer separately
    # — it owns its own cancellation / drain logic rather
    # than the stop() / task-join pattern the pollers use.
    mux = app.bot_data.get("turn_stream_mux")
    if mux is not None:
        await mux.stop()


def _make_renderer_factory(
    app: Application[Any, Any, Any, Any, Any, Any],
) -> Callable[[str, str], TurnRenderer]:
    """Build a :type:`~.turn_stream.RendererFactory` bound to
    this PTB app.

    Closure captures ``app`` so each new :class:`TurnRenderer`
    gets a live handle to the PTB bot for posting + editing
    messages, and a keyboard builder that produces the same
    callback-data shape as the existing
    :class:`ApprovalPoller` flow — so approval taps in the
    renderer-posted bubbles resolve through the same
    :func:`_on_approval_callback` handler.

    ``chat_id`` is pulled off the multiplexer's session→chat
    map, which the chat handler populates via ``ensure``
    right before dispatching a chat request."""

    def _factory(session_id: str, turn_id: str) -> TurnRenderer:
        mux = app.bot_data.get("turn_stream_mux")
        chat_id: int | None = None
        if mux is not None:
            chat_id = mux.chat_id_for(session_id)
        if chat_id is None:
            # No chat_id recorded — a turn landed on the SSE
            # before any chat was sent from the bot (cron
            # firings for the same session, or a developer
            # kicking a turn via curl). Drop the renderer's
            # bubbles on the floor by pointing at a bogus
            # chat_id 0 and letting PTB's send_message fail
            # with a logged warning. Not pretty but
            # correct: we don't know where to put them.
            chat_id = 0
        return TurnRenderer(
            bot=app.bot,
            chat_id=chat_id,
            turn_id=turn_id,
            build_approval_keyboard=_approval_keyboard,
        )

    return _factory


def _approval_keyboard(approval_id: str) -> InlineKeyboardMarkup:
    """Produce the approval inline keyboard. Matches the shape
    ``ApprovalPoller`` / :func:`_make_on_prompt` build so the
    existing :func:`_on_approval_callback` handler resolves
    taps on renderer-posted bubbles too — same callback-data
    format, same decide POST."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve", callback_data=build_callback_data("approve", approval_id)
                ),
                InlineKeyboardButton(
                    "❌ Reject", callback_data=build_callback_data("reject", approval_id)
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔓 Trust session",
                    callback_data=build_callback_data("trust_session", approval_id),
                )
            ],
        ]
    )


def _make_on_event(
    app: Application[Any, Any, Any, Any, Any, Any],
) -> Callable[[int, dict[str, Any], str], Awaitable[None]]:
    """Return an ``on_event`` callback bound to this PTB app's
    bot handle. The pusher uses this to deliver a formatted
    event body as a regular Telegram message.

    No inline keyboard, no callback data — these are
    notifications, not decisions. The approval flow has its own
    pipeline via :class:`ApprovalPoller`."""

    async def _on_event(user_id: int, _entry: dict[str, Any], text: str) -> None:
        try:
            await app.bot.send_message(chat_id=user_id, text=text)
        except Exception as e:
            # Let the pusher log + move on; re-raise so the
            # pusher's own try/except captures this for
            # "telegram.event_pusher.send_failed".
            _log.debug(
                "telegram.event_push_send_failed",
                extra={"user_id": user_id, "error": str(e)},
            )
            raise

    return _on_event


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
    # Drop the inline keyboard so the prompt is in a terminal
    # state: a successful decide shouldn't invite re-tapping,
    # and a failed decide (typically 404 on a stale prompt left
    # over from a previous gateway run) shouldn't invite
    # re-tapping either — each retry would produce the same
    # 404 and dirty up the logs. Passing `reply_markup=None`
    # replaces any existing markup with "no keyboard."
    outcome = f"✅ {decision.replace('_', ' ')}" if ok else f"⚠️ Failed: {detail or 'unknown error'}"
    msg = query.message
    if isinstance(msg, Message):
        try:
            original_text = msg.text or ""
            await context.bot.edit_message_text(
                chat_id=msg.chat_id,
                message_id=msg.message_id,
                text=f"{original_text}\n\n{outcome}",
                reply_markup=None,
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


async def _on_lastturn(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_lastturn_command(bot, update, services)


async def _on_text(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_text(bot, update, services)


async def _on_text_ptb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text-handler wrapper that primes the Phase 4.8b turn
    stream for this chat's session BEFORE dispatching to the
    pure handler. ``mux.ensure`` is idempotent — called on
    every message, opens an SSE connection on first use,
    refreshes the session→chat_id binding on each call."""
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
    mux: TurnStreamMultiplexer | None = context.bot_data.get("turn_stream_mux")
    if mux is not None:
        prefs = services.prefs.get(parsed.chat_id)
        try:
            await mux.ensure(prefs.session_id, parsed.chat_id)
        except Exception as exc:
            # SSE subscription failures shouldn't stop the
            # user from getting a chat reply — the growing-
            # bubble UX degrades gracefully to the legacy
            # single-message reply path.
            _log.warning(
                "telegram.turn_stream.ensure_failed",
                extra={
                    "session_id": prefs.session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
    await _on_text(context.bot, parsed, services)


async def _on_voice(bot: Any, update: IncomingUpdate, services: Services) -> None:
    await handlers.handle_voice(bot, update)

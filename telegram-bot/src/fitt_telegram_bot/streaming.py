"""Helper that turns a stream of deltas into Telegram message edits.

Telegram rate-limits edits to about 1 per second per chat; we keep
at least ``MIN_EDIT_INTERVAL_S`` between edits and always flush on
``finalize()``.

The caller sends an empty placeholder message first, then hands the
returned ``message_id`` plus a live stream of deltas to this
``StreamingEditor``. It accumulates the deltas and edits the
placeholder in place.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine

_log = logging.getLogger(__name__)

MIN_EDIT_INTERVAL_S = 0.8
"""Minimum gap between successive Telegram edit_message_text calls
for the same chat. Stays well under Telegram's 1 edit/sec per-chat
limit while still feeling live."""


class StreamingEditor:
    """Accumulate text chunks and edit a Telegram message in place."""

    def __init__(
        self,
        bot: TelegramBotAPI,
        chat_id: int,
        message_id: int,
        *,
        min_interval_s: float = MIN_EDIT_INTERVAL_S,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._buffer: list[str] = []
        self._min_interval = min_interval_s
        self._last_edit_ts: float = 0.0
        self._dirty = False

    async def append(self, delta: str) -> None:
        if not delta:
            return
        self._buffer.append(delta)
        self._dirty = True
        now = time.monotonic()
        if now - self._last_edit_ts >= self._min_interval:
            await self._flush(now)

    async def finalize(self) -> None:
        """Flush the accumulator one last time."""
        if self._dirty:
            await self._flush(time.monotonic(), force=True)
        elif not self._buffer:
            # Nothing ever arrived; leave a dash so the user sees
            # something instead of the empty placeholder.
            try:
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text="(no content)",
                )
            except Exception as e:
                _log.warning(
                    "telegram.finalize_empty_edit_failed",
                    extra={"error": str(e)},
                )

    async def _flush(self, now: float, *, force: bool = False) -> None:
        text = "".join(self._buffer)
        if not text:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._message_id,
                text=text[-4000:],  # Telegram caps at 4096 chars
            )
            self._last_edit_ts = now
            self._dirty = False
        except Exception as e:
            _log.warning(
                "telegram.edit_failed",
                extra={
                    "chat_id": self._chat_id,
                    "message_id": self._message_id,
                    "force": force,
                    "error": str(e),
                },
            )


class TelegramBotAPI:
    """Minimal structural type for the bot operations we use.

    Declared as a protocol-ish class for typing; the real object is
    ``telegram.Bot`` from ``python-telegram-bot``.
    """

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        raise NotImplementedError


# Small helper so tests can await a coroutine in a sync context
# without needing an event loop hack.
def run(coro: Coroutine[object, object, object]) -> object:
    return asyncio.get_event_loop().run_until_complete(coro)

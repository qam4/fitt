"""Phase 4.8b — SSE subscriber for per-turn events.

Bridges the gateway's ``GET /v1/sessions/<id>/turns/stream``
SSE endpoint to per-turn :class:`~.turn_renderer.TurnRenderer`
instances. One subscriber per session, opened lazily when a
chat is about to be initiated in that session and kept alive
across multiple turns.

Lifecycle
---------

* A chat handler calls :meth:`TurnStreamMultiplexer.ensure`
  before dispatching a chat request. The multiplexer opens
  an SSE connection to the session's stream URL if none is
  already running. Idempotent — subsequent calls for the
  same session are a no-op.
* Inside the SSE loop, every event is routed to a
  :class:`TurnRenderer` keyed by the event's ``turn_id``.
  Fresh turn_ids spawn a new renderer using the factory the
  caller supplied (which knows how to construct a keyboard
  + a Telegram bot handle for the right chat).
* ``turn_finished`` retires the renderer: it runs its
  ``finalize``, gets the finish footer posted, and the
  renderer entry is dropped from the multiplexer's state
  so it doesn't leak memory across the lifetime of the
  gateway connection.
* Transport failures (connection drop, gateway restart) are
  recovered with exponential backoff: 0.5s → 1s → 2s → …
  capped at 30s. The in-memory renderer state for any
  in-flight turn is lost on disconnect — the JSONL on the
  gateway is the source of truth; a future admin dashboard
  can reconstruct past turns from disk. See design.md §
  "Failure modes."

Cancellation
------------

:meth:`TurnStreamMultiplexer.stop` cancels every running
subscriber task. Called from the bot's ``post_shutdown``
hook so Ctrl-C on the bot process doesn't leak httpx
connections.

What this module does NOT do
----------------------------

* It doesn't render text-token deltas from the chat
  completions endpoint. Those tokens arrive via
  :meth:`GatewayClient.chat` and are pushed into the
  renderer from the chat handler via
  :meth:`~.turn_renderer.TurnRenderer.append_reply_text`.
  The multiplexer owns only the SSE side of the state.
* It doesn't handle inline-keyboard approval callbacks.
  Those still flow through the existing callback handler
  in ``bot.py`` which POSTs to ``/v1/approvals/<id>/decide``
  — the renderer's approval bubble contains the same
  keyboard, same callback data shape, same flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .turn_renderer import TurnRenderer

_log = logging.getLogger(__name__)


_RECONNECT_MIN_BACKOFF_S = 0.5
_RECONNECT_MAX_BACKOFF_S = 30.0
_SSE_READ_TIMEOUT_S = 120.0
"""httpx read timeout for the streaming GET. Generous — the
endpoint has a 15s heartbeat which resets this; a timeout
fires only on a genuinely stuck connection."""


RendererFactory = Callable[[str, str], TurnRenderer]
"""(session_id, turn_id) -> TurnRenderer.

Injected so this module doesn't import the Telegram bot
directly. Real wiring builds a renderer bound to the chat
associated with the session (looked up via
:class:`fitt_telegram_bot.prefs.PrefsStore`), and with a
keyboard builder that matches the existing approval
callback-data shape."""


@dataclass
class _SessionSubscriber:
    """Per-session SSE task bookkeeping."""

    session_id: str
    task: asyncio.Task[None]
    # turn_id -> live TurnRenderer for that turn's state.
    renderers: dict[str, TurnRenderer] = field(default_factory=dict)


class TurnStreamMultiplexer:
    """Owns SSE subscriptions, one per session in use.

    Thread-unsafe by design — intended to live on the bot's
    asyncio loop. All public methods are coroutines.
    """

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        renderer_factory: RendererFactory,
        client_tag: str = "telegram",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "X-FITT-Client": client_tag,
            "Accept": "text/event-stream",
        }
        self._renderer_factory = renderer_factory
        self._subscribers: dict[str, _SessionSubscriber] = {}
        self._stopped = False
        # Latest chat_id seen for each session. The renderer
        # factory reads this to bind new TurnRenderers to the
        # right Telegram chat. v1 assumption: one chat per
        # session. If a user sends from a different chat after
        # switching sessions, the newer chat wins.
        self._chat_ids: dict[str, int] = {}

    # ------------------------------------------------ public

    async def ensure(self, session_id: str, chat_id: int) -> None:
        """Start an SSE subscriber for ``session_id`` if one
        isn't already running.

        ``chat_id`` records the Telegram chat the renderer's
        bubbles should post to. Later calls can update this
        (re-binding to a new chat after a ``/session`` switch
        from a different chat). Called from the chat handler
        immediately before dispatching a chat request.
        Idempotent; safe to call on every message."""
        self._chat_ids[session_id] = chat_id
        if self._stopped:
            return
        sub = self._subscribers.get(session_id)
        if sub is not None and not sub.task.done():
            return
        task = asyncio.create_task(
            self._run(session_id),
            name=f"turn-stream-{session_id}",
        )
        self._subscribers[session_id] = _SessionSubscriber(session_id=session_id, task=task)
        _log.info(
            "turn_stream.started",
            extra={"session_id": session_id, "chat_id": chat_id},
        )

    def chat_id_for(self, session_id: str) -> int | None:
        """Return the most-recently-recorded chat_id for
        ``session_id``, or ``None`` if ``ensure`` hasn't been
        called yet. Used by the renderer factory inside
        ``bot.py``."""
        return self._chat_ids.get(session_id)

    def get_renderer(self, session_id: str, turn_id: str) -> TurnRenderer | None:
        """Return the live renderer for the given turn if one
        exists. Used by the chat handler to push reply-token
        deltas into the growing bubble."""
        sub = self._subscribers.get(session_id)
        if sub is None:
            return None
        return sub.renderers.get(turn_id)

    async def stop(self) -> None:
        """Cancel every running subscriber. Called from the
        bot's ``post_shutdown`` hook."""
        self._stopped = True
        for sub in list(self._subscribers.values()):
            if not sub.task.done():
                sub.task.cancel()
        # Await cancellations with a short timeout; don't let
        # a stuck subscriber block shutdown.
        for sub in list(self._subscribers.values()):
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(sub.task, timeout=2.0)
        self._subscribers.clear()

    # ------------------------------------------------ internal

    async def _run(self, session_id: str) -> None:
        """Long-lived loop: open SSE, dispatch events, reconnect
        on failure."""
        backoff = _RECONNECT_MIN_BACKOFF_S
        while not self._stopped:
            try:
                await self._consume_once(session_id)
                # Clean EOF — gateway closed the stream. Don't
                # immediately reconnect in a tight loop; back off
                # the minimum so we don't flood a restarting
                # gateway.
                backoff = _RECONNECT_MIN_BACKOFF_S
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError as exc:
                _log.warning(
                    "turn_stream.connection_lost",
                    extra={
                        "session_id": session_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "backoff_s": backoff,
                    },
                )
            except Exception as exc:
                _log.warning(
                    "turn_stream.unexpected_error",
                    extra={
                        "session_id": session_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "backoff_s": backoff,
                    },
                )
            # Drop any in-flight renderers — we missed the tail
            # of their turns. Next events (if any) will create
            # fresh renderers, same behaviour as design.md's
            # "drop in-memory state on reconnect" rule.
            sub = self._subscribers.get(session_id)
            if sub is not None:
                sub.renderers.clear()
            if self._stopped:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, _RECONNECT_MAX_BACKOFF_S)

    async def _consume_once(self, session_id: str) -> None:
        """One connection attempt. Returns normally on clean
        EOF; raises on transport failure (caller handles the
        reconnect)."""
        url = f"{self._base}/v1/sessions/{session_id}/turns/stream"
        timeout = httpx.Timeout(_SSE_READ_TIMEOUT_S, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout, headers=self._headers) as http:
            async with http.stream("GET", url) as response:
                if response.status_code // 100 != 2:
                    _log.warning(
                        "turn_stream.bad_status",
                        extra={
                            "session_id": session_id,
                            "status": response.status_code,
                        },
                    )
                    # Treat as transient; backoff outer loop will
                    # wait and retry.
                    raise httpx.HTTPError(f"status={response.status_code}")
                _log.info("turn_stream.connected", extra={"session_id": session_id})
                async for line in response.aiter_lines():
                    await self._handle_line(session_id, line)

    async def _handle_line(self, session_id: str, line: str) -> None:
        """Parse one SSE wire line and dispatch if it's a
        data frame.

        SSE format: a frame ends at ``\\n\\n``; each line inside
        is a ``field: value`` pair. httpx's ``aiter_lines``
        already splits on line boundaries, so we accumulate
        the ``data:`` values until we see an empty line
        (frame terminator)."""
        # Cheap stateless parse: a ``data: <json>`` line by
        # itself is a complete single-line frame (how the
        # gateway emits them). We don't handle multi-line
        # data fields because the gateway doesn't produce
        # them. Comments (``: heartbeat``) and ``event:`` type
        # hints get skipped here — the frame's JSON payload
        # is the full story.
        if not line.startswith("data:"):
            return
        payload = line[len("data:") :].lstrip()
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            _log.warning(
                "turn_stream.malformed_frame",
                extra={"session_id": session_id, "payload_sample": payload[:120]},
            )
            return
        await self._dispatch_event(session_id, event)

    async def _dispatch_event(self, session_id: str, event: dict[str, Any]) -> None:
        """Route one event to the renderer for its turn."""
        turn_id = str(event.get("turn_id", ""))
        if not turn_id:
            return
        sub = self._subscribers.get(session_id)
        if sub is None:
            return
        renderer = sub.renderers.get(turn_id)
        if renderer is None:
            renderer = self._renderer_factory(session_id, turn_id)
            sub.renderers[turn_id] = renderer
        try:
            await renderer.handle_event(event)
        except Exception as exc:
            _log.warning(
                "turn_stream.renderer_error",
                extra={
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "kind": event.get("kind", ""),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        # Drop renderer on turn_finished so we don't carry
        # dead state forever.
        if event.get("kind") == "turn_finished":
            sub.renderers.pop(turn_id, None)


# --------------------------------------------------------------- exports


__all__ = [
    "RendererFactory",
    "TurnStreamMultiplexer",
]


# Intentional no-op to keep the Awaitable import used. Removed
# in a future pass when the factory type starts awaiting.
_ = Awaitable

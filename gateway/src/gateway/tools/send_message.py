"""Phase 4.5 Task 6 — ``send_message`` inline tool.

Lets the agent proactively push a message to the user outside the
normal request/response cadence. Lands a ``agent_message`` event
on the event log; Task 7's Telegram push subscriber picks it up
and delivers. When no push channel is configured, the event still
lands (``fitt inbox`` shows it) and we log a one-time WARNING so
the operator knows delivery is CLI-only.

Auto bucket. The tool is low-risk by construction — the *worst*
case is a model that spams messages, which we cap with a sliding-
window rate limiter per session. Rate-limit rejections come back
as a structured tool error with ``retry_after_secs`` in the
payload so the model can back off rather than retrying
immediately.

Configuration
-------------

Configured under ``tools.send_message`` in ``config.yaml``:

.. code-block:: yaml

    tools:
      send_message:
        window_secs: 60
        max_per_window: 10

Defaults match the design doc (60-second window, 10 messages max).
A stricter ceiling is reasonable for chatty models; loosen for
long-running crons that legitimately emit many progress pings.

Design choices
--------------

* **Sliding window, per session.** The window is owned by the tool
  (the builder captures a closure); the registry / app never sees
  it. That keeps the ToolContext clean — this is the only
  stateful inline tool today and we'd rather isolate its state
  than grow the shared context for a single consumer.
* **Rate-limit response shape.** A ``ToolResult.error`` carrying
  a JSON-ish payload with ``retry_after_secs`` so the model's
  chat-time reasoning can notice and wait, instead of whatever
  opaque "error" it would see if we raised.
* **No-channel warning once.** A module-level flag flipped the
  first time the tool runs without a push channel. The event
  still lands either way — the warning is operator UX, not a
  guard.
* **Push-channel detection via config.** The warning path needs
  to know whether a Telegram bot is configured. We read the
  config off ``ctx.policy``'s owner — which we can't do
  directly. Instead the builder takes a
  ``push_channel_available`` callable so app.py can wire in the
  same heuristic the chat handler uses, keeping the "is there a
  push channel" decision in one place.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable
from time import monotonic
from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- defaults


_DEFAULT_WINDOW_SECS = 60.0
_DEFAULT_MAX_PER_WINDOW = 10


_TEXT_CAP_CHARS = 4000
"""Cap on the ``text`` field so a runaway model can't try to push
a 100 KB essay through a Telegram message. Slightly larger than
the events.telegram_body_cap default (3500) so the cap here
never trims before the push formatter does — easier to debug
truncation that way."""


# --------------------------------------------------------------- schema


_SCHEMA_SEND_MESSAGE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "Message body delivered to the user. Keep it short "
                "— Telegram previews ~100 chars; anything over 3500 "
                "gets truncated by the push formatter."
            ),
        },
        "title": {
            "type": "string",
            "description": (
                "Optional one-line header shown before the body. "
                "Useful for grouping (e.g. 'Build finished:')."
            ),
            "default": "",
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- rate limiter


class SendMessageRateLimiter:
    """Per-session sliding-window rate limiter.

    Keeps a ``deque`` of send timestamps per session; on each
    attempt, drops timestamps older than the window, then
    compares len against the ceiling.

    Thread-safety: we only mutate from inside the asyncio loop
    so no lock is needed. Multiple cooperating tasks in the same
    loop can't interleave a mid-operation pop/append.
    """

    __slots__ = ("_max_per_window", "_per_session", "_window_secs")

    def __init__(self, window_secs: float, max_per_window: int) -> None:
        self._window_secs = float(window_secs)
        self._max_per_window = int(max_per_window)
        self._per_session: dict[str, deque[float]] = {}

    @property
    def window_secs(self) -> float:
        return self._window_secs

    @property
    def max_per_window(self) -> int:
        return self._max_per_window

    def try_acquire(self, session_key: str, *, now: float | None = None) -> tuple[bool, float]:
        """Attempt to record one send for ``session_key``.

        Returns ``(accepted, retry_after_secs)``. On accept,
        ``retry_after_secs`` is ``0.0``; on reject, it's the
        number of seconds until the oldest in-window send drops
        out, i.e. the earliest moment another send would be
        accepted.

        ``now`` is injectable for tests; production uses
        :func:`time.monotonic` (steady clock, unaffected by wall-
        clock adjustments)."""
        ts = monotonic() if now is None else now
        bucket = self._per_session.setdefault(session_key, deque())
        cutoff = ts - self._window_secs
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= self._max_per_window:
            oldest = bucket[0]
            retry_after = (oldest + self._window_secs) - ts
            # Clamp to a positive float. In theory the while-loop
            # above already ensures oldest >= cutoff, but defend
            # against floating-point jitter at the boundary.
            return False, max(retry_after, 0.01)
        bucket.append(ts)
        return True, 0.0

    # Inspection helpers for tests + future introspection tools.

    def current(self, session_key: str) -> int:
        """How many in-window sends this session has made."""
        bucket = self._per_session.get(session_key)
        if not bucket:
            return 0
        cutoff = monotonic() - self._window_secs
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return len(bucket)

    def reset(self, session_key: str | None = None) -> None:
        """Drop all state (optionally for a single session). Used
        by tests and by the future ``fitt session new`` teardown
        path — starting a fresh session gives the limiter a clean
        slate so the old session's counts don't leak."""
        if session_key is None:
            self._per_session.clear()
            return
        self._per_session.pop(session_key, None)


# --------------------------------------------------------------- builder


def build_send_message_tool(
    *,
    limiter: SendMessageRateLimiter | None = None,
    push_channel_available: Callable[[], bool] | None = None,
) -> Tool:
    """Return the ``send_message`` Tool.

    ``limiter`` defaults to a fresh limiter with the design's
    defaults. Callers (``app.py``) usually pass one built from
    config so operators can tune the window.

    ``push_channel_available`` is a zero-arg callable returning
    whether a push subscriber (today: Telegram) is configured.
    We call it lazily inside the tool so config reloads and the
    ``MCPManager``-style "subscribers may come and go" posture
    both work. When ``None``, the warning path is skipped
    entirely — primarily for test contexts.
    """
    limiter = limiter or SendMessageRateLimiter(
        window_secs=_DEFAULT_WINDOW_SECS,
        max_per_window=_DEFAULT_MAX_PER_WINDOW,
    )

    # Flipped on first call that finds no push channel so the
    # warning fires exactly once per gateway process (per the
    # spec — model can call send_message many times per session
    # and we don't want a warning storm).
    warned_no_channel = False

    async def _send_message_impl(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        nonlocal warned_no_channel

        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult.error("'text' is required and must be non-empty")
        if len(text) > _TEXT_CAP_CHARS:
            return ToolResult.error(
                f"'text' exceeds {_TEXT_CAP_CHARS} chars (got {len(text)}); trim the message"
            )
        title = args.get("title") or ""
        if not isinstance(title, str):
            return ToolResult.error("'title' must be a string")

        # Pick up any operator-tuned window/ceiling from config.
        # We re-instantiate only when the values differ from the
        # limiter's current settings so a hot config reload can
        # push a new ceiling without a gateway restart. This is
        # cheap — building a fresh limiter is O(1).
        _maybe_update_limiter_from_policy(limiter, ctx)

        accepted, retry_after = limiter.try_acquire(ctx.session_key)
        if not accepted:
            # Structured error so the model can parse retry_after.
            payload = {
                "error": "rate_limit_exceeded",
                "message": (
                    f"send_message exceeded {limiter.max_per_window} "
                    f"messages in {limiter.window_secs:.0f}s for this "
                    f"session. Wait and try again, or tell the user "
                    f"what's happening in the normal reply."
                ),
                "retry_after_secs": round(retry_after, 2),
                "window_secs": limiter.window_secs,
                "max_per_window": limiter.max_per_window,
            }
            return ToolResult.error(json.dumps(payload))

        # Event emission. The event log is the source of truth;
        # push delivery (Telegram) is a separate subscriber that
        # Task 7 wires. Missing event log is a test-time
        # situation; in production app.py always wires one, but
        # if it's None we still take the rate-limit slot so the
        # model can't spam the tool as a free no-op.
        if ctx.events is None:
            return ToolResult.error(
                "send_message unavailable: event log not wired. "
                "This is a gateway misconfiguration; message was "
                "not delivered or logged."
            )

        from ..events import new_entry as new_event

        try:
            ctx.events.append(
                new_event(
                    kind="agent_message",
                    session_key=ctx.session_key,
                    title=title.strip() or "Agent Message",
                    body=text,
                    meta={
                        "tool": "send_message",
                        "client": ctx.client,
                    },
                )
            )
        except Exception as exc:
            _log.warning(
                "send_message.event_emit_failed",
                extra={"session": ctx.session_key, "error": str(exc)},
            )
            return ToolResult.error(f"send_message: failed to record event: {exc}")

        # No-push-channel warning, exactly once per process.
        if push_channel_available is not None and not warned_no_channel:
            try:
                channel_ok = bool(push_channel_available())
            except Exception as exc:
                _log.debug(
                    "send_message.push_channel_probe_failed",
                    extra={"error": str(exc)},
                )
                channel_ok = True  # assume OK; don't spam the warning
            if not channel_ok:
                warned_no_channel = True
                _log.warning(
                    "send_message.no_push_channel",
                    extra={
                        "session": ctx.session_key,
                        "note": (
                            "no Telegram / push subscriber "
                            "configured; agent_message events are "
                            "only visible via `fitt inbox`"
                        ),
                    },
                )

        return ToolResult.ok("sent")

    return Tool(
        name="send_message",
        description=(
            "Proactively send a text message to the user, outside "
            "the normal reply channel. Use for state-change "
            "notifications from a silent cron, progress pings on "
            "a long-running task, or any moment the user isn't "
            "watching this chat but should know something. Each "
            "session is rate-limited; exceeding the cap returns a "
            "structured error with retry_after_secs."
        ),
        schema=_SCHEMA_SEND_MESSAGE,
        callable=_send_message_impl,
        default_bucket=ApprovalBucket.AUTO,
    )


# --------------------------------------------------------------- policy glue


def _maybe_update_limiter_from_policy(limiter: SendMessageRateLimiter, ctx: ToolContext) -> None:
    """If ``config.yaml`` sets ``tools.send_message.window_secs`` /
    ``max_per_window``, propagate those to the limiter.

    Cheap to call per tool invocation — we only overwrite when
    the value differs, so a config that doesn't set these knobs
    is a no-op after the first call. Read via ``per_tool_extras``
    to stay consistent with the ``http_get.deny_hosts`` pattern;
    no new ToolPolicy field needed."""
    if ctx.policy is None:
        return
    extras = ctx.policy.per_tool_extras.get("send_message")
    if not extras:
        return
    window = extras.get("window_secs")
    ceiling = extras.get("max_per_window")
    try:
        if window is not None and float(window) > 0:
            limiter._window_secs = float(window)
        if ceiling is not None and int(ceiling) > 0:
            limiter._max_per_window = int(ceiling)
    except (TypeError, ValueError) as exc:
        _log.warning(
            "send_message.bad_policy_value",
            extra={"error": str(exc), "extras": extras},
        )

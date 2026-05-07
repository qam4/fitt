"""Phase 4.5 Task 7 — Telegram event push subscriber.

Polls ``GET /v1/events`` on the gateway and delivers new entries
to the allowlisted Telegram users. Runs as a background task
alongside :class:`ApprovalPoller`; one polling loop each, both on
the same PTB event loop (PTB's `concurrent_updates` enables
parallel dispatch, but these tasks don't need it — they're their
own coroutines).

Why poll instead of push:

The gateway and bot are separate processes (Phase 3.5 Docker
layout) so an in-process "EventLog.append fires the pusher" hook
isn't available. Polling every second matches the approval
poller's shape and keeps the wire contract simple (one HTTP
endpoint, no long-lived websocket).

Cursor semantics:

* On first boot with no cursor file, we start from "now" — the
  gateway's event log may be months old and flooding the user
  with history on restart is hostile UX. Historical delivery is
  ``fitt inbox`` (task 9), not this pipeline.
* After each tick we record the newest ``ts`` we saw. Next tick
  asks for events strictly after it. Bot restart reloads the
  cursor from disk.
* ``approval_requested`` events are explicitly skipped here —
  approvals are surfaced by the dedicated :class:`ApprovalPoller`
  with an inline keyboard. Delivering them twice would be
  duplicate-spam and the wrong UI.

On-disk state at ``$FITT_HOME/telegram/pusher_cursor.json``:

.. code-block:: json

    {"last_ts": 1778177143.801154}

Missing or corrupted file = start-from-now. Writes are atomic
via tempfile + ``os.replace``. One file, one field — not trying
to grow more state here; anything more complex lives in a proper
store.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events_push import format_event
from .gateway_client import GatewayClient

_log = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL_S = 1.0
"""Match ApprovalPoller — one second is a fine human-scale latency
and keeps the HTTP traffic negligible (one small GET per second)."""


# Event kinds that have their own delivery surface and should NOT
# be double-delivered via this push pipeline. Today: approvals go
# through the inline-keyboard ApprovalPoller.
_SKIP_KINDS: frozenset[str] = frozenset({"approval_requested", "approval_resolved"})


class _CursorStore:
    """Atomic persistence for the last-seen event timestamp.

    Tiny helper; kept inline here rather than as a separate module
    because it has exactly one consumer and the schema is one
    field. If we grow a second cursored-poller, factor out."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> float | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            ts = data.get("last_ts")
            if isinstance(ts, int | float):
                return float(ts)
        except (OSError, json.JSONDecodeError) as e:
            _log.warning(
                "telegram.pusher.cursor_read_failed",
                extra={"error": str(e), "path": str(self._path)},
            )
        return None

    def save(self, ts: float) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix="pusher-cursor-",
            suffix=".json.tmp",
            dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"last_ts": ts}, fh)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


@dataclass
class EventPusher:
    """Polls the gateway's event log and pushes new entries to
    Telegram.

    Construction is cheap; :meth:`run` is the hot loop. Cursor is
    loaded on construction, written after every successful tick
    that saw new events."""

    gateway: GatewayClient
    allowlist: frozenset[int]
    on_event: Callable[[int, dict[str, Any], str], Awaitable[None]]
    """Called once per new event, per allowlisted user. Signature
    ``(user_id, raw_event_dict, formatted_text)``. The concrete
    bot wires this to ``bot.send_message``. Takes the formatted
    text as a separate argument so tests can assert on the
    formatting without needing to call the formatter themselves
    (and so custom subscribers can override the formatter if they
    want)."""

    cursor_path: Path
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S

    _cursor: _CursorStore = field(init=False)
    _last_ts: float = field(init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self._cursor = _CursorStore(self.cursor_path)
        loaded = self._cursor.load()
        # First boot / missing cursor: start from "now" so we
        # don't flood the user with backlog from events.jsonl.
        # Flooding would be the more polite failure mode, but in
        # practice the log is long enough (crons, agent_messages,
        # cron_fired bookkeeping) that replaying it on every
        # restart is worse than missing one.
        self._last_ts = loaded if loaded is not None else time.time()
        _log.info(
            "telegram.pusher.initialised",
            extra={
                "last_ts": self._last_ts,
                "cursor_existed": loaded is not None,
                "allowlist_size": len(self.allowlist),
            },
        )

    # ---------------------------------------------- lifecycle

    async def run(self) -> None:
        _log.info("telegram.event_pusher.started")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    _log.warning(
                        "telegram.event_pusher.tick_failed",
                        extra={"error": str(e)},
                    )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.poll_interval_s,
                    )
                except TimeoutError:
                    pass
        finally:
            _log.info("telegram.event_pusher.stopped")

    def stop(self) -> None:
        self._stop.set()

    # ---------------------------------------------- internals

    async def _tick(self) -> None:
        events = await self.gateway.list_events(since=self._last_ts)
        if not events:
            return

        # Events come back sorted by ts (file order = write order
        # = chronological). Process in order so our cursor advance
        # is always monotonic.
        newest_ts = self._last_ts
        for entry in events:
            try:
                ts = float(entry.get("ts", 0.0))
            except (TypeError, ValueError):
                continue
            if ts <= self._last_ts:
                # Defensive: the gateway's ``since`` filter is
                # strict-greater, but a clock glitch or replay
                # could still surface equal-ts entries. Skip them
                # to preserve at-most-once-per-restart semantics.
                continue

            kind = entry.get("kind", "")
            if kind in _SKIP_KINDS:
                newest_ts = max(newest_ts, ts)
                continue

            text = format_event(entry)
            if not text:
                # Formatter returned empty (typically a silent
                # cron_completed with no body). Nothing to push,
                # but still advance the cursor so we don't revisit.
                newest_ts = max(newest_ts, ts)
                continue

            for user_id in self.allowlist:
                try:
                    await self.on_event(user_id, entry, text)
                except Exception as e:
                    # A delivery failure for one user shouldn't
                    # stop us from advancing the cursor — the
                    # event is recorded in the gateway's log
                    # regardless, and the bot will keep trying
                    # on subsequent events. Per spec 7d: delivery
                    # failures logged, do not block the producer.
                    _log.warning(
                        "telegram.event_pusher.send_failed",
                        extra={
                            "user_id": user_id,
                            "kind": kind,
                            "error": str(e),
                        },
                    )
            newest_ts = max(newest_ts, ts)

        if newest_ts > self._last_ts:
            self._last_ts = newest_ts
            try:
                self._cursor.save(newest_ts)
            except Exception as e:
                # Cursor persistence failure isn't fatal; we'll
                # re-push on next restart, which is annoying but
                # not broken.
                _log.warning(
                    "telegram.pusher.cursor_save_failed",
                    extra={"error": str(e)},
                )


__all__ = ["EventPusher"]

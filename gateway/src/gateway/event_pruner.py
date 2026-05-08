"""Phase 4.5 Task 10 — daily event-log pruner.

A small async loop that wakes once every ``poll_interval_secs``
(default 6h), checks whether a day has elapsed since the last
prune, and if so runs :meth:`EventLog.prune` with the
configured ``max_age_days``. Emits a ``system_pruned`` event
after each run so ``fitt inbox`` shows there wasn't a gap.

Why not a :class:`gateway.cron.CronService` job?
-----------------------------------------------

The spec says "built-in cron (not user-visible in
``cron.json``)". We keep this out of ``cron.json`` for three
reasons:

1. **Not user-visible.** Users listing ``fitt cron list``
   shouldn't see an internal maintenance task; it's not
   something they can meaningfully tweak.
2. **Different firing semantics.** User crons spawn a full
   agent session via :class:`~gateway.cron_runner.CronRunner`
   (fresh memory, stubs, dispatch). The pruner is a plain
   function call. Reusing the runner for a no-op agent
   session would be wasteful and noisy in the event log
   (every prune would also emit ``cron_fired`` /
   ``cron_completed``).
3. **Restart-safe by construction.** If the gateway never
   stays up for 24h, the pruner never runs, and nothing in
   the event log is ever older than 24h of runtime anyway.
   The pruner matters on a NAS that stays up for weeks.

Shape mirrors :class:`~gateway.cron_scheduler.CronScheduler`:
``start`` / ``stop`` background task; ``tick`` is the
test-facing entry point that runs one iteration. The scheduler's
pattern has already earned its place; the pruner follows it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .events import EventLog, new_entry

_log = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL_SECS = 6 * 3600.0
"""Six hours. Low enough that a 24h-rotation target stays on
schedule even with a slightly drifted last-prune anchor; high
enough that the loop barely shows up in logs."""

_DEFAULT_PRUNE_INTERVAL_SECS = 24 * 3600.0
"""Run at most once per day. The event log grows at ~events/day
so pruning more often is a waste of syscalls."""


class EventPruner:
    """Background pruner for :class:`EventLog`.

    Stateless apart from the running task handle and the
    ``last_pruned_ts`` anchor — tracked in-memory (restarts
    reset it, which is fine: the first tick after boot will
    prune if needed and record fresh state)."""

    def __init__(
        self,
        *,
        events: EventLog,
        max_age_days: int,
        poll_interval_secs: float = _DEFAULT_POLL_INTERVAL_SECS,
        prune_interval_secs: float = _DEFAULT_PRUNE_INTERVAL_SECS,
        anchor_path: Path | None = None,
    ) -> None:
        self._events = events
        self._max_age_days = max_age_days
        self._poll_interval_secs = poll_interval_secs
        self._prune_interval_secs = prune_interval_secs
        self._anchor_path = anchor_path
        self._last_pruned_ts: float = self._load_anchor()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Launch the pruner's background loop. Safe to call
        twice — a second call is a no-op if already running."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="event-pruner")
        _log.info(
            "events.pruner.started",
            extra={
                "max_age_days": self._max_age_days,
                "poll_interval_secs": self._poll_interval_secs,
            },
        )

    async def stop(self) -> None:
        """Signal the loop to wind down and wait for it."""
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                _log.warning("events.pruner.stop_timeout — cancelling")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    # -------------------------------------------------- loop

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception as e:  # pragma: no cover - defensive
                _log.exception("events.pruner.tick_failed", extra={"error": str(e)})
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval_secs)
            except TimeoutError:
                continue

    async def tick(self, *, now: float | None = None) -> int | None:
        """One pruner iteration. Returns the count removed if a
        prune ran, ``None`` if it was skipped (not yet due).

        Tests drive this directly rather than running ``_loop``.
        """
        current = now if now is not None else time.time()
        if current - self._last_pruned_ts < self._prune_interval_secs:
            return None
        removed = self._events.prune(max_age_days=self._max_age_days, now=current)
        self._last_pruned_ts = current
        self._save_anchor(current)
        # Emit a visible event so ``fitt inbox`` shows the prune
        # happened — without this, an operator looking at a day
        # with zero other events might wonder if the pruner is
        # broken.
        self._events.append(
            new_entry(
                ts=current,
                kind="system_pruned",
                session_key="system",
                title="Event log pruned",
                body=f"Removed {removed} entries older than {self._max_age_days} days.",
                meta={
                    "removed": removed,
                    "max_age_days": self._max_age_days,
                },
            )
        )
        return removed

    # -------------------------------------------------- anchor persistence

    def _load_anchor(self) -> float:
        """Read the last-pruned timestamp off disk if available.

        Without this, every gateway restart would re-schedule the
        next prune 24h out, and on a bot that restarts several
        times a day the prune would never run. The anchor is a
        single float in a tiny file; losing it is harmless
        (worst case: one extra prune on next tick).
        """
        if self._anchor_path is None or not self._anchor_path.exists():
            return 0.0
        try:
            return float(self._anchor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError) as e:
            _log.warning(
                "events.pruner.anchor_load_failed",
                extra={"path": str(self._anchor_path), "error": str(e)},
            )
            return 0.0

    def _save_anchor(self, ts: float) -> None:
        """Persist the last-pruned timestamp for restart resume."""
        if self._anchor_path is None:
            return
        try:
            self._anchor_path.parent.mkdir(parents=True, exist_ok=True)
            self._anchor_path.write_text(f"{ts}\n", encoding="utf-8")
        except OSError as e:
            _log.warning(
                "events.pruner.anchor_save_failed",
                extra={"path": str(self._anchor_path), "error": str(e)},
            )


def default_anchor_path(fitt_home: Path) -> Path:
    """Where to store the last-pruned timestamp."""
    return fitt_home / "events.pruner.anchor"

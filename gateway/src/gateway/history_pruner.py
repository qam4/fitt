"""Phase 5 Task 9 — history pruner.

Daily background task that walks
``$FITT_HOME/sessions/*/history/*.md`` and deletes files older
than ``memory.history_max_days`` (default 90). Emits a
``system_pruned`` event so operators see the prune happened
in ``fitt inbox``.

Mirrors :class:`gateway.event_pruner.EventPruner` — same
async-loop + anchor-file + tick-test-hook shape. Chose not to
share a base class because the two prune different things
(events vs files) with different budget semantics; a one-file
duplication is cheaper to reason about than a shared parent
everyone has to learn."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .events import EventLog, new_entry

_log = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL_SECS = 6 * 3600.0
"""Mirror of the event pruner cadence. See its comment."""

_DEFAULT_PRUNE_INTERVAL_SECS = 24 * 3600.0
"""Once per day."""


class HistoryPruner:
    """Background pruner for session history files.

    Walks ``sessions/<s>/history/*.md`` for every session and
    deletes files whose filename date is older than
    ``max_age_days``. Files whose filename doesn't parse as a
    date are left alone (possibly hand-edited or a future
    schema; better to surprise the operator with "my backup
    file is still there" than delete it)."""

    def __init__(
        self,
        *,
        sessions_dir: Path,
        events: EventLog,
        max_age_days: int,
        poll_interval_secs: float = _DEFAULT_POLL_INTERVAL_SECS,
        prune_interval_secs: float = _DEFAULT_PRUNE_INTERVAL_SECS,
        anchor_path: Path | None = None,
    ) -> None:
        self._sessions_dir = sessions_dir
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
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="history-pruner")
        _log.info(
            "history.pruner.started",
            extra={
                "max_age_days": self._max_age_days,
                "poll_interval_secs": self._poll_interval_secs,
            },
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                _log.warning("history.pruner.stop_timeout — cancelling")
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
                _log.exception(
                    "history.pruner.tick_failed",
                    extra={"error": str(e)},
                )
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_interval_secs,
                )
            except TimeoutError:
                continue

    async def tick(self, *, now: float | None = None) -> int | None:
        """One pruner iteration. Returns the count of files
        removed if a prune ran, or ``None`` if it was skipped
        (not yet due).

        Tests drive this directly rather than running ``_loop``
        so the cadence stays deterministic."""
        current = now if now is not None else time.time()
        if current - self._last_pruned_ts < self._prune_interval_secs:
            return None
        removed = self._prune_files(current)
        self._last_pruned_ts = current
        self._save_anchor(current)
        # Emit a visible marker so a day with no other activity
        # still shows the prune happened.
        self._events.append(
            new_entry(
                ts=current,
                kind="system_pruned",
                session_key="system",
                title=f"History pruned ({removed} file(s))",
                body=(f"Removed {removed} history file(s) older than {self._max_age_days} days."),
                meta={
                    "target": "history",
                    "removed": removed,
                    "max_age_days": self._max_age_days,
                },
            )
        )
        return removed

    # -------------------------------------------------- internals

    def _prune_files(self, now_ts: float) -> int:
        """Walk every session's history directory and delete
        files whose filename date is older than the cutoff.

        Also walks each session's ``artifacts/<YYYY-MM-DD>/``
        tree and drops over-age day directories in full. Tool-
        output artifacts share the same retention window as
        history by design: they were produced by tool calls
        whose surrounding turns are aging out at the same
        cadence, so keeping them around after the history is
        gone leaves orphaned blobs nobody can re-contextualise.

        Non-parseable filenames (e.g. ``backup.md``) are left
        alone — operators place recovery artifacts in these
        directories and we don't want to eat them."""
        if not self._sessions_dir.exists():
            return 0
        cutoff_day = datetime.fromtimestamp(now_ts, tz=UTC).date() - timedelta(
            days=self._max_age_days
        )
        removed = 0
        for session_dir in self._sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            history_dir = session_dir / "history"
            if history_dir.is_dir():
                for f in history_dir.iterdir():
                    if not f.is_file() or f.suffix != ".md":
                        continue
                    try:
                        file_day = date.fromisoformat(f.stem)
                    except ValueError:
                        # Not a YYYY-MM-DD file; leave it alone.
                        continue
                    if file_day < cutoff_day:
                        try:
                            f.unlink()
                            removed += 1
                        except OSError as e:
                            _log.warning(
                                "history.pruner.unlink_failed",
                                extra={"file": str(f), "error": str(e)},
                            )
            artifacts_dir = session_dir / "artifacts"
            if artifacts_dir.is_dir():
                for day_dir in artifacts_dir.iterdir():
                    if not day_dir.is_dir():
                        continue
                    try:
                        day = date.fromisoformat(day_dir.name)
                    except ValueError:
                        # Unrecognised dir name; leave alone.
                        continue
                    if day < cutoff_day:
                        removed += _remove_tree(day_dir)
        return removed

    def _load_anchor(self) -> float:
        if self._anchor_path is None or not self._anchor_path.exists():
            return 0.0
        try:
            return float(self._anchor_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError) as e:
            _log.warning(
                "history.pruner.anchor_load_failed",
                extra={"path": str(self._anchor_path), "error": str(e)},
            )
            return 0.0

    def _save_anchor(self, ts: float) -> None:
        if self._anchor_path is None:
            return
        try:
            self._anchor_path.parent.mkdir(parents=True, exist_ok=True)
            self._anchor_path.write_text(f"{ts}\n", encoding="utf-8")
        except OSError as e:
            _log.warning(
                "history.pruner.anchor_save_failed",
                extra={"path": str(self._anchor_path), "error": str(e)},
            )


# --------------------------------------------------------------- helpers


def default_history_anchor_path(fitt_home: Path) -> Path:
    return fitt_home / "history.pruner.anchor"


def _remove_tree(path: Path) -> int:
    """Recursively delete ``path`` and return the number of
    files removed (directories don't count toward the removed
    counter — we mirror the per-file semantics of the history
    sweep so operators reading the event log see a meaningful
    count).

    Errors are logged and swallowed per-file so one unreadable
    artifact doesn't block the rest of the day directory from
    draining. Empty directories left behind after a partial
    sweep get cleaned up on the next pass."""
    removed = 0
    for child in path.iterdir() if path.is_dir() else []:
        if child.is_dir():
            removed += _remove_tree(child)
        elif child.is_file():
            try:
                child.unlink()
                removed += 1
            except OSError as e:
                _log.warning(
                    "history.pruner.unlink_failed",
                    extra={"file": str(child), "error": str(e)},
                )
    try:
        path.rmdir()
    except OSError as e:
        # Non-empty (due to unlink failures above) or otherwise
        # stubborn. Leave it for the next pass rather than
        # failing the sweep.
        _log.warning(
            "history.pruner.rmdir_failed",
            extra={"dir": str(path), "error": str(e)},
        )
    return removed

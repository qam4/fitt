"""Phase 4.5 scheduler loop — fires due cron jobs.

Keeps the :class:`~gateway.cron.CronService` data model separate
from the time-driven loop. The service owns state; this module
owns "when is a job due, and what do we do when it's due."

Core idea:

* Every ``poll_interval_secs`` (default 30), wake up, ask the
  service which jobs are due, and fire each one as an
  independent asyncio task. A hung firing doesn't stall the
  scheduler: ``asyncio.wait_for`` applies ``timeout_secs`` per
  firing, and everything else runs in parallel tasks.
* "Due" means ``next_run(after=last_run_ts or created_ts) <= now``.
  We track ``last_run_ts`` on the job record so restarts don't
  re-fire everything at once.
* One-shot ``at`` schedules with ``delete_after_run=True`` are
  removed from the service after their firing completes, so a
  manual "remind me in 30 minutes" cron self-cleans.
* The fire callback is an async function supplied by the
  gateway at startup. Task 5 wires it to the agent session
  spawner; this module just calls whatever it's given.

Shape matches the existing ``MCPManager``: ``start_all`` launches
a background task, ``stop_all`` cancels it. Tests can poke the
internals directly without spinning up the loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .cron import CronJob, CronService

_log = logging.getLogger(__name__)

FireCallback = Callable[[CronJob], Awaitable[None]]
"""The hook the gateway hands the scheduler to do the real work
when a job is due. Task 4 allows any async callable; task 5
wires it to the agent-session runner."""


class CronScheduler:
    """Runs the periodic tick that fires due cron jobs.

    Stateless apart from the running task handle and a map of
    ``job_id -> in-flight task`` so we don't double-fire a job
    whose previous run hasn't returned yet (common on a slow
    polling-style cron: every 60s firing that takes 120s would
    otherwise pile up until OOM).
    """

    def __init__(
        self,
        service: CronService,
        *,
        on_fire: FireCallback,
        poll_interval_secs: float = 30.0,
        firing_timeout_secs: float = 1800.0,
    ) -> None:
        self._service = service
        self._on_fire = on_fire
        self._poll_interval_secs = poll_interval_secs
        self._firing_timeout_secs = firing_timeout_secs
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._in_flight: dict[str, asyncio.Task[None]] = {}

    # -------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Launch the periodic tick as a background task. Safe to
        call twice — a second call is a no-op if already running.
        """
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="cron-scheduler")
        _log.info(
            "cron.scheduler.started",
            extra={
                "poll_interval_secs": self._poll_interval_secs,
                "firing_timeout_secs": self._firing_timeout_secs,
            },
        )

    async def stop(self) -> None:
        """Signal the loop to wind down and wait for it. Any
        in-flight firings are cancelled with a warning — we
        don't wait indefinitely for a hung fire callback."""
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError:
                _log.warning("cron.scheduler.stop_timeout — cancelling")
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        for task in list(self._in_flight.values()):
            task.cancel()
        self._in_flight.clear()

    # -------------------------------------------------- loop

    async def _loop(self) -> None:
        """Main tick. Runs until ``stop`` signals the event."""
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception as e:  # pragma: no cover - defensive
                _log.exception("cron.scheduler.tick_failed", extra={"error": str(e)})
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval_secs)
            except TimeoutError:
                # Normal path: the wait timed out so we loop back
                # and tick again.
                continue

    async def tick(self, *, now: float | None = None) -> list[str]:
        """One scan pass. Returns the ids of jobs whose fire
        task was launched (or relaunched) this tick.

        Tests drive this directly rather than running ``_loop``
        to keep the scheduler's time-driven behaviour
        deterministic. The production path calls ``tick`` on
        each ``_loop`` iteration.
        """
        current = now if now is not None else time.time()
        self._service.reload_if_changed()
        fired: list[str] = []
        for job in self._service.list(include_disabled=False):
            if not self._is_due(job, current):
                continue
            if job.id in self._in_flight and not self._in_flight[job.id].done():
                # Previous firing still running; skip this tick to
                # avoid piling up. The scheduler logs it so a
                # chronically-slow cron is visible.
                _log.warning(
                    "cron.scheduler.skip_overlap",
                    extra={"cron_id": job.id, "cron_name": job.name},
                )
                continue
            self._in_flight[job.id] = asyncio.create_task(
                self._fire_one(job),
                name=f"cron-fire-{job.id}",
            )
            fired.append(job.id)
        # Opportunistic cleanup of done tasks so the dict doesn't
        # grow unbounded on a long-running process.
        for id_ in [i for i, t in self._in_flight.items() if t.done()]:
            del self._in_flight[id_]
        return fired

    # -------------------------------------------------- due check

    def _is_due(self, job: CronJob, now: float) -> bool:
        """A job is due when its next scheduled firing (anchored
        at ``last_run_ts`` or ``created_ts``) is <= now.

        For ``at`` schedules, this collapses to the obvious check
        on ``schedule.at_ts``.
        """
        if job.schedule.kind == "at":
            at_ts = job.schedule.at_ts
            return at_ts is not None and at_ts <= now and job.last_run_ts is None
        anchor = job.last_run_ts if job.last_run_ts is not None else job.created_ts
        next_ts = job.schedule.next_run(after=anchor)
        return next_ts is not None and next_ts <= now

    # -------------------------------------------------- firing

    async def _fire_one(self, job: CronJob) -> None:
        """Wrap the hook in a timeout, record the outcome on the
        job record, and (for one-shot ``at`` schedules) remove
        the job after success."""
        fire_started = time.time()
        try:
            await asyncio.wait_for(
                self._on_fire(job),
                timeout=self._firing_timeout_secs,
            )
        except TimeoutError:
            _log.warning(
                "cron.scheduler.firing_timeout",
                extra={"cron_id": job.id, "timeout_secs": self._firing_timeout_secs},
            )
            self._mark_run(job, ok=False, err="timeout")
            return
        except asyncio.CancelledError:
            # Scheduler was asked to stop mid-firing. Don't
            # record a failure — the gateway's going down.
            raise
        except Exception as e:
            _log.exception(
                "cron.scheduler.firing_failed",
                extra={"cron_id": job.id, "error": str(e)},
            )
            self._mark_run(job, ok=False, err=f"{type(e).__name__}: {e}")
            return
        duration = time.time() - fire_started
        _log.info(
            "cron.scheduler.firing_ok",
            extra={"cron_id": job.id, "duration_secs": round(duration, 2)},
        )
        self._mark_run(job, ok=True, err="")
        # One-shot cleanup. Do this *after* marking the run so
        # the last_status is recorded (useful for debugging via
        # the audit log even though the job record is gone).
        if job.delete_after_run:
            self._service.remove(job.id)

    def _mark_run(self, job: CronJob, *, ok: bool, err: str) -> None:
        """Update last_run_ts / last_status / last_error on the
        persisted record. Best-effort — a write failure shouldn't
        crash the firing task."""
        try:
            self._service.update(
                job.id,
                last_run_ts=time.time(),
                last_status="ok" if ok else "error",
                last_error=err,
            )
        except Exception as e:  # pragma: no cover - defensive
            _log.warning(
                "cron.scheduler.mark_run_failed",
                extra={"cron_id": job.id, "error": str(e)},
            )

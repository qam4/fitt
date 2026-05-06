"""Tests for Phase 4.5 Task 4 — cron scheduler loop.

We drive the scheduler's ``tick`` method directly rather than
running ``_loop`` + relying on wall-clock sleeps: deterministic
and fast. The ``_loop`` timer-wakeup behaviour is exercised
indirectly via start/stop lifecycle tests.

Four concerns:

* Due detection: every / at / cron jobs become due at the right
  moment and not before.
* Overlap protection: a slow firing doesn't pile up.
* Timeout: a firing that never returns is cancelled and the
  record captures the failure.
* One-shot cleanup: ``at`` schedules with ``delete_after_run``
  are removed after a successful firing.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from gateway.cron import CronJob, CronSchedule, CronService
from gateway.cron_scheduler import CronScheduler

# --------------------------------------------------------------- fixtures


@pytest.fixture
def svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "cron.json")


def _mk_every(svc: CronService, *, every_secs: int = 60, created_ts: float = 0.0) -> CronJob:
    job = CronJob(
        id="",
        name="t",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=every_secs),
    )
    if created_ts:
        job.created_ts = created_ts
    return svc.add(job)


def _mk_at(svc: CronService, *, at_ts: float) -> CronJob:
    job = CronJob(
        id="",
        name="one-shot",
        message="m",
        schedule=CronSchedule(kind="at", at_ts=at_ts),
    )
    return svc.add(job)


# --------------------------------------------------------------- due detection


async def test_every_job_is_not_due_before_interval(svc: CronService) -> None:
    job = _mk_every(svc, every_secs=60, created_ts=1000.0)
    sched = CronScheduler(svc, on_fire=_noop)
    fired = await sched.tick(now=1050.0)
    assert fired == []
    # last_run_ts not touched either.
    assert svc.get(job.id).last_run_ts is None  # type: ignore[union-attr]


async def test_every_job_fires_after_interval(svc: CronService) -> None:
    job = _mk_every(svc, every_secs=60, created_ts=1000.0)
    observed: list[str] = []

    async def capture(j: CronJob) -> None:
        observed.append(j.id)

    sched = CronScheduler(svc, on_fire=capture)
    fired = await sched.tick(now=1060.0)
    # Wait for the background task to finish.
    await _drain(sched)
    assert fired == [job.id]
    assert observed == [job.id]
    # last_run_ts recorded so next tick doesn't re-fire.
    reloaded = svc.get(job.id)
    assert reloaded is not None
    assert reloaded.last_run_ts is not None
    assert reloaded.last_status == "ok"


async def test_every_job_does_not_refire_immediately(svc: CronService) -> None:
    """Once last_run_ts is stamped, the next firing must wait
    for another interval. Regression guard against 'tick twice
    right after tick()' bugs."""
    _mk_every(svc, every_secs=60, created_ts=1000.0)
    sched = CronScheduler(svc, on_fire=_noop)
    await sched.tick(now=1060.0)
    await _drain(sched)
    # Second tick at the same "now" — should not fire again.
    again = await sched.tick(now=1060.0)
    assert again == []


async def test_at_job_fires_once_and_gets_deleted(svc: CronService) -> None:
    job = _mk_at(svc, at_ts=1000.0)
    assert job.delete_after_run is True
    sched = CronScheduler(svc, on_fire=_noop)
    fired = await sched.tick(now=1001.0)
    await _drain(sched)
    assert fired == [job.id]
    # Cron is gone.
    assert svc.get(job.id) is None


async def test_at_job_not_due_before_timestamp(svc: CronService) -> None:
    _mk_at(svc, at_ts=2000.0)
    sched = CronScheduler(svc, on_fire=_noop)
    fired = await sched.tick(now=1999.9)
    assert fired == []


async def test_cron_expression_is_due(svc: CronService) -> None:
    """Daily 9 AM UTC. created_ts at epoch 0 → first firing is
    at 32400. Tick at 32400 fires; tick at 32399 doesn't."""
    job = svc.add(
        CronJob(
            id="",
            name="daily",
            message="m",
            schedule=CronSchedule(kind="cron", cron_expr="0 9 * * *", timezone="UTC"),
        )
    )
    # Force created_ts=0 so the next-run math is predictable.
    svc.update(job.id, last_run_ts=None)
    # Hack: CronService doesn't expose a created_ts setter — go
    # direct on the in-memory record for this test.
    svc._state.jobs[job.id].created_ts = 0.0  # type: ignore[attr-defined]

    sched = CronScheduler(svc, on_fire=_noop)
    assert await sched.tick(now=32399.0) == []
    fired = await sched.tick(now=32401.0)
    await _drain(sched)
    assert fired == [job.id]


async def test_disabled_job_is_not_due(svc: CronService) -> None:
    job = _mk_every(svc, every_secs=60, created_ts=1000.0)
    svc.set_enabled(job.id, False)
    sched = CronScheduler(svc, on_fire=_noop)
    fired = await sched.tick(now=1_000_000.0)
    assert fired == []


# --------------------------------------------------------------- overlap


async def test_overlap_is_skipped(svc: CronService) -> None:
    """If the previous firing is still running when the next
    tick arrives, skip the new firing rather than pile up."""
    job = _mk_every(svc, every_secs=10, created_ts=1000.0)
    hang = asyncio.Event()

    async def slow(_j: CronJob) -> None:
        await hang.wait()

    sched = CronScheduler(svc, on_fire=slow)
    # First firing launches and blocks on `hang`.
    first = await sched.tick(now=1010.0)
    assert first == [job.id]
    # Second tick while the first is still pending: NOT fired.
    second = await sched.tick(now=1020.0)
    assert second == []
    # Clean up: let the in-flight task finish so the test
    # doesn't leave an unawaited coroutine.
    hang.set()
    await _drain(sched)


# --------------------------------------------------------------- timeout


async def test_firing_timeout_is_captured(svc: CronService) -> None:
    job = _mk_every(svc, every_secs=60, created_ts=1000.0)

    async def never_returns(_j: CronJob) -> None:
        await asyncio.sleep(3600)

    sched = CronScheduler(
        svc,
        on_fire=never_returns,
        firing_timeout_secs=0.05,
    )
    await sched.tick(now=1060.0)
    await _drain(sched)
    reloaded = svc.get(job.id)
    assert reloaded is not None
    assert reloaded.last_status == "error"
    assert "timeout" in reloaded.last_error


async def test_firing_exception_is_captured(svc: CronService) -> None:
    job = _mk_every(svc, every_secs=60, created_ts=1000.0)

    async def boom(_j: CronJob) -> None:
        raise ValueError("kaboom")

    sched = CronScheduler(svc, on_fire=boom)
    await sched.tick(now=1060.0)
    await _drain(sched)
    reloaded = svc.get(job.id)
    assert reloaded is not None
    assert reloaded.last_status == "error"
    assert "kaboom" in reloaded.last_error


# --------------------------------------------------------------- lifecycle


async def test_start_stop_roundtrip(svc: CronService) -> None:
    """start() launches the loop; stop() cancels and waits."""
    sched = CronScheduler(svc, on_fire=_noop, poll_interval_secs=0.02)
    await sched.start()
    # Let the loop run a few ticks.
    await asyncio.sleep(0.1)
    await sched.stop()
    # Second stop is a no-op.
    await sched.stop()


async def test_start_is_idempotent(svc: CronService) -> None:
    sched = CronScheduler(svc, on_fire=_noop, poll_interval_secs=1.0)
    await sched.start()
    await sched.start()
    await sched.stop()


async def test_start_fires_a_job_inside_the_loop(svc: CronService) -> None:
    """Smoke test that the running loop actually picks up due
    jobs, not just the directly-driven tick path."""
    observed: list[str] = []

    async def capture(j: CronJob) -> None:
        observed.append(j.id)

    # Set up a job due right now.
    job = _mk_every(svc, every_secs=1, created_ts=time.time() - 10)

    sched = CronScheduler(svc, on_fire=capture, poll_interval_secs=0.02)
    await sched.start()
    # Wait long enough for at least one poll.
    await asyncio.sleep(0.2)
    await sched.stop()
    assert job.id in observed


# --------------------------------------------------------------- helpers


async def _noop(_j: CronJob) -> None:
    return None


async def _drain(sched: CronScheduler) -> None:
    """Wait for any in-flight firings launched by the most
    recent tick to complete."""
    tasks = [t for t in sched._in_flight.values() if not t.done()]  # type: ignore[attr-defined]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# Silence an unused-import warning from strict mypy when the
# Any import isn't actually referenced.
_: Any = None

"""Phase 4.5 Task 10 — event pruner.

Covers the four invariants from the pruner's docstring:

* **Prunes old entries.** Entries older than ``max_age_days`` go
  away; newer ones stay.
* **Cadence is respected.** Back-to-back ticks within
  ``prune_interval_secs`` skip the second.
* **Anchor persists across restarts.** Writing a fresh
  ``EventPruner`` with the same anchor path resumes from where
  the previous instance left off.
* **Emits ``system_pruned`` event.** ``fitt inbox`` sees the
  prune happened even on days with no other activity.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.event_pruner import EventPruner, default_anchor_path
from gateway.events import EventLog, new_entry


def _seed_events(log: EventLog, *, old_ts: float, new_ts: float) -> None:
    """Write one stale + one fresh entry to the log."""
    log.append(
        new_entry(
            ts=old_ts,
            kind="cron_completed",
            session_key="stale",
            title="old firing",
        )
    )
    log.append(
        new_entry(
            ts=new_ts,
            kind="cron_completed",
            session_key="fresh",
            title="new firing",
        )
    )


async def test_tick_prunes_old_entries_and_emits_event(tmp_path: Path) -> None:
    """First invariant: old entries go; new entries stay; a
    ``system_pruned`` event lands."""
    log = EventLog(tmp_path / "events.jsonl")
    now = 10_000_000.0
    old = now - 95 * 86400  # 95 days → older than cutoff
    new = now - 1 * 86400  # 1 day → well within
    _seed_events(log, old_ts=old, new_ts=new)

    pruner = EventPruner(
        events=log,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=now)
    assert removed == 1

    remaining = log.read()
    # Two should remain: the fresh cron_completed and the
    # system_pruned entry the pruner itself emitted.
    kinds = sorted(e.kind for e in remaining)
    assert kinds == ["cron_completed", "system_pruned"]
    pruned_evt = next(e for e in remaining if e.kind == "system_pruned")
    assert pruned_evt.meta["removed"] == 1
    assert pruned_evt.meta["max_age_days"] == 90


async def test_tick_skips_when_run_too_recently(tmp_path: Path) -> None:
    """Second invariant: a tick within ``prune_interval_secs`` of
    the last prune returns ``None`` (didn't run). Operators who
    wire the pruner to a 1-minute poller still get daily prunes
    and not minute-by-minute file rewrites."""
    log = EventLog(tmp_path / "events.jsonl")
    now = 10_000_000.0
    _seed_events(log, old_ts=now - 100 * 86400, new_ts=now - 1 * 86400)

    pruner = EventPruner(
        events=log,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    first = await pruner.tick(now=now)
    assert first == 1

    # An hour later — still inside the 24h prune interval.
    second = await pruner.tick(now=now + 3600)
    assert second is None, (
        "second tick within 24h of the first should be a no-op; "
        "returning a count would mean we pruned twice in a day"
    )


async def test_tick_runs_again_after_interval(tmp_path: Path) -> None:
    """Same as above, but >24h later the pruner runs again."""
    log = EventLog(tmp_path / "events.jsonl")
    now = 10_000_000.0
    _seed_events(log, old_ts=now - 100 * 86400, new_ts=now - 1 * 86400)

    pruner = EventPruner(
        events=log,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    await pruner.tick(now=now)

    # Write a new stale entry between the two ticks.
    log.append(
        new_entry(
            ts=now - 120 * 86400,
            kind="cron_completed",
            session_key="stale-2",
            title="ancient firing",
        )
    )

    # 25h later — past the interval.
    removed = await pruner.tick(now=now + 25 * 3600)
    assert removed == 1


async def test_anchor_persists_across_restarts(tmp_path: Path) -> None:
    """Third invariant: a fresh pruner instance with the same
    anchor path reads the persisted timestamp and doesn't
    re-prune until the interval has elapsed."""
    log = EventLog(tmp_path / "events.jsonl")
    anchor = tmp_path / "anchor"
    now = 10_000_000.0
    _seed_events(log, old_ts=now - 100 * 86400, new_ts=now - 1 * 86400)

    first = EventPruner(events=log, max_age_days=90, anchor_path=anchor)
    await first.tick(now=now)
    assert anchor.exists(), "anchor should be written after the first prune"

    # Simulate a restart: construct a new pruner with the same
    # anchor path and try to prune an hour later.
    second = EventPruner(events=log, max_age_days=90, anchor_path=anchor)
    skipped = await second.tick(now=now + 3600)
    assert skipped is None, (
        "a new pruner instance must honour the persisted anchor; "
        "otherwise every gateway restart would re-prune the log"
    )


async def test_default_anchor_path_under_fitt_home(tmp_path: Path) -> None:
    assert default_anchor_path(tmp_path) == tmp_path / "events.pruner.anchor"


async def test_tick_empty_log_is_a_no_op(tmp_path: Path) -> None:
    """Empty log + first tick: returns 0, emits the
    system_pruned event (zero removed) so the audit trail is
    honest about the work done."""
    log = EventLog(tmp_path / "events.jsonl")
    pruner = EventPruner(
        events=log,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=10_000_000.0)
    assert removed == 0

    entries = log.read()
    kinds = [e.kind for e in entries]
    assert kinds == ["system_pruned"]


async def test_anchor_load_tolerates_corruption(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt anchor file (non-numeric content) shouldn't
    crash the pruner — it should log a warning and fall back to
    zero (first prune runs on the next tick)."""
    anchor = tmp_path / "anchor"
    anchor.write_text("not-a-number\n", encoding="utf-8")

    log = EventLog(tmp_path / "events.jsonl")
    pruner = EventPruner(events=log, max_age_days=90, anchor_path=anchor)
    # Internal state fell back to 0 → a first tick runs.
    removed = await pruner.tick(now=10_000_000.0)
    assert removed == 0
    # And we logged about the corruption.
    assert any("anchor_load_failed" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]

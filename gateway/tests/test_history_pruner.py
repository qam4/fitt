"""Phase 5 Task 9 — history pruner.

Mirrors ``test_event_pruner``'s shape since the two share the
same async + anchor + tick contract. Scenarios covered:

* **Retention boundary.** Files within the window kept;
  outside removed.
* **Multiple sessions.** Each session's history directory is
  walked independently.
* **Cadence.** Back-to-back ticks within the prune interval
  skip the second.
* **Anchor persistence.** A fresh pruner with the same anchor
  path picks up the previous last-pruned timestamp.
* **Event emission.** Each prune run lands a ``system_pruned``
  event with ``meta.target="history"``.
* **Non-date filenames preserved.** Operator-placed backups
  (``backup.md``) aren't touched by the date-based sweep.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from gateway.events import EventLog
from gateway.history_pruner import HistoryPruner, default_history_anchor_path


def _seed(dirpath: Path, day: date, content: str = "test\n") -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / f"{day.isoformat()}.md"
    path.write_text(content, encoding="utf-8")
    return path


def _ts(day: date) -> float:
    return datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()


async def test_tick_removes_files_past_retention(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    history = sessions / "main" / "history"
    now_day = date(2026, 5, 8)

    old_path = _seed(history, now_day - timedelta(days=95))
    fresh_path = _seed(history, now_day - timedelta(days=10))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=_ts(now_day))
    assert removed == 1
    assert not old_path.exists()
    assert fresh_path.exists()


async def test_tick_walks_multiple_sessions(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    now_day = date(2026, 5, 8)
    old_a = _seed(sessions / "session_a" / "history", now_day - timedelta(days=100))
    old_b = _seed(sessions / "session_b" / "history", now_day - timedelta(days=150))
    fresh = _seed(sessions / "session_a" / "history", now_day - timedelta(days=5))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=_ts(now_day))
    assert removed == 2
    assert not old_a.exists()
    assert not old_b.exists()
    assert fresh.exists()


async def test_tick_emits_system_pruned_event(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    history = sessions / "main" / "history"
    now_day = date(2026, 5, 8)
    _seed(history, now_day - timedelta(days=100))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    await pruner.tick(now=_ts(now_day))

    entries = events.read()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "system_pruned"
    assert entry.meta["target"] == "history"
    assert entry.meta["removed"] == 1
    assert entry.meta["max_age_days"] == 90


async def test_tick_skips_within_interval(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    history = sessions / "main" / "history"
    now_day = date(2026, 5, 8)
    _seed(history, now_day - timedelta(days=100))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    first = await pruner.tick(now=_ts(now_day))
    assert first == 1
    # Ten minutes later → skip.
    second = await pruner.tick(now=_ts(now_day) + 600)
    assert second is None


async def test_tick_runs_again_after_interval(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    history = sessions / "main" / "history"
    now_day = date(2026, 5, 8)
    _seed(history, now_day - timedelta(days=100))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    await pruner.tick(now=_ts(now_day))

    # Seed another old file for the second pass.
    _seed(history, now_day - timedelta(days=200))

    # 25 hours later → second prune runs.
    second = await pruner.tick(now=_ts(now_day) + 25 * 3600)
    assert second == 1


async def test_anchor_persists_across_restarts(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    history = sessions / "main" / "history"
    anchor = tmp_path / "anchor"
    now_day = date(2026, 5, 8)
    _seed(history, now_day - timedelta(days=100))

    events = EventLog(tmp_path / "events.jsonl")
    first = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=anchor,
    )
    await first.tick(now=_ts(now_day))
    assert anchor.exists()

    # Fresh instance, same anchor — shouldn't re-prune within
    # the interval.
    second = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=anchor,
    )
    result = await second.tick(now=_ts(now_day) + 600)
    assert result is None


async def test_non_date_filenames_preserved(tmp_path: Path) -> None:
    """Operator-placed backup files shouldn't get caught by
    the date-based sweep. If the stem isn't a valid ISO date,
    we leave the file alone."""
    sessions = tmp_path / "sessions"
    history = sessions / "main" / "history"
    history.mkdir(parents=True)
    backup = history / "backup.md"
    backup.write_text("operator put this here\n", encoding="utf-8")
    # A real date file too, for a sanity check.
    now_day = date(2026, 5, 8)
    old = _seed(history, now_day - timedelta(days=100))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    await pruner.tick(now=_ts(now_day))

    assert backup.exists(), "backup.md should be untouched"
    assert not old.exists(), "YYYY-MM-DD file past retention should be gone"


async def test_empty_sessions_dir_is_no_op(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    # Don't create the sessions dir.
    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=_ts(date(2026, 5, 8)))
    assert removed == 0


def test_default_history_anchor_path(tmp_path: Path) -> None:
    assert default_history_anchor_path(tmp_path) == tmp_path / "history.pruner.anchor"


# --------------------------------------------------------------- artifacts


async def test_tick_sweeps_old_artifact_day_directories(tmp_path: Path) -> None:
    """Artifacts under ``sessions/<k>/artifacts/<YYYY-MM-DD>/``
    share the same retention window as history files. An old
    day directory gets removed (tree and all); a fresh one is
    left alone.

    This is the pairing with the tool-output hoisting layer: a
    tool produced a big file 100 days ago, the history turn
    that referenced it is aging out, the artifact should age
    out with it."""
    sessions = tmp_path / "sessions"
    now_day = date(2026, 5, 8)
    # Seed a session with both an old and a fresh day of
    # artifacts. Seed a harmless history file so the sweep
    # actually runs (removal count wouldn't be zero).
    old_day = (now_day - timedelta(days=100)).isoformat()
    fresh_day = (now_day - timedelta(days=5)).isoformat()
    old_dir = sessions / "main" / "artifacts" / old_day
    old_dir.mkdir(parents=True)
    (old_dir / "read_file-abc.txt").write_text("old blob", encoding="utf-8")
    (old_dir / "project_shell-def.txt").write_text("old log", encoding="utf-8")
    fresh_dir = sessions / "main" / "artifacts" / fresh_day
    fresh_dir.mkdir(parents=True)
    (fresh_dir / "read_file-xyz.txt").write_text("keep me", encoding="utf-8")
    _seed(sessions / "main" / "history", now_day - timedelta(days=10))

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=_ts(now_day))
    # Two artifact files removed; history file is within
    # retention so it doesn't contribute.
    assert removed == 2
    assert not old_dir.exists()
    assert fresh_dir.exists()
    assert (fresh_dir / "read_file-xyz.txt").exists()


async def test_tick_ignores_unrecognised_artifact_dirs(tmp_path: Path) -> None:
    """A directory whose name isn't ``YYYY-MM-DD`` gets left
    alone — same posture as the history sweep's handling of
    non-date filenames. Operators occasionally place recovery
    artifacts in these dirs; we shouldn't eat them."""
    sessions = tmp_path / "sessions"
    now_day = date(2026, 5, 8)
    weird_dir = sessions / "main" / "artifacts" / "manual-backup"
    weird_dir.mkdir(parents=True)
    (weird_dir / "note.txt").write_text("keep", encoding="utf-8")

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    await pruner.tick(now=_ts(now_day))
    assert weird_dir.exists()
    assert (weird_dir / "note.txt").exists()


# --------------------------------------------------------------- turns


async def test_tick_sweeps_old_turn_event_files(tmp_path: Path) -> None:
    """Per-turn event logs under
    ``sessions/<k>/turns/<YYYY-MM-DD>.jsonl`` age out on the
    same 90-day window as history files. Rationale: when a
    turn's conversation history is gone, the fine-grained
    per-iteration trace that belonged to it is rubble too."""
    sessions = tmp_path / "sessions"
    now_day = date(2026, 5, 8)
    turns_dir = sessions / "main" / "turns"
    turns_dir.mkdir(parents=True)
    old_path = turns_dir / f"{(now_day - timedelta(days=100)).isoformat()}.jsonl"
    old_path.write_text('{"kind": "turn_started"}\n', encoding="utf-8")
    fresh_path = turns_dir / f"{(now_day - timedelta(days=10)).isoformat()}.jsonl"
    fresh_path.write_text('{"kind": "turn_started"}\n', encoding="utf-8")

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    removed = await pruner.tick(now=_ts(now_day))
    assert removed == 1
    assert not old_path.exists()
    assert fresh_path.exists()


async def test_tick_preserves_non_date_turn_filenames(tmp_path: Path) -> None:
    """Same posture as the history sweep: non-``YYYY-MM-DD``
    filenames under ``turns/`` are left alone."""
    sessions = tmp_path / "sessions"
    now_day = date(2026, 5, 8)
    turns_dir = sessions / "main" / "turns"
    turns_dir.mkdir(parents=True)
    weird = turns_dir / "backup.jsonl"
    weird.write_text('{"note": "operator saved this"}\n', encoding="utf-8")

    events = EventLog(tmp_path / "events.jsonl")
    pruner = HistoryPruner(
        sessions_dir=sessions,
        events=events,
        max_age_days=90,
        anchor_path=tmp_path / "anchor",
    )
    await pruner.tick(now=_ts(now_day))
    assert weird.exists()

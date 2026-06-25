"""Tests for the cross-phase task rollup parser/classifier."""

from __future__ import annotations

from gateway.task_rollup import (
    classify_open,
    parse_status,
    parse_tasks_md,
    roll_up,
)

_SAMPLE = """\
# Tasks: Phase X

## Phase Xa - thing

- [x] 1. Build the thing.
  DONE: shipped it.
- [ ] 2. Wire the other thing into create_app.
- [ ] 3. Verify with `netstat -an` that 8080 is bound. (At-home, runtime.)
- [ ] 4. QNAP pilot step. (Deferred to pilot.)
  - sub-bullet that should not be counted as its own task
- [ ] 5. A genuinely open dev task with continuation
  spanning multiple lines of detail.
"""


def test_parse_states_and_ids() -> None:
    tasks = parse_tasks_md("phaseX", _SAMPLE)
    ids = [t.id for t in tasks]
    assert ids == ["1", "2", "3", "4", "5"]
    by_id = {t.id: t for t in tasks}
    assert by_id["1"].state == "done"
    assert by_id["2"].state == "open"
    # Title is the first-line text after the id.
    assert by_id["2"].title.startswith("Wire the other thing")


def test_classification() -> None:
    tasks = {t.id: t for t in parse_tasks_md("phaseX", _SAMPLE)}
    assert tasks["1"].kind == "done"
    assert tasks["2"].kind == "open"
    assert tasks["3"].kind == "at_home"  # (At-home, runtime.)
    assert tasks["4"].kind == "deferred"  # (Deferred to pilot.)
    assert tasks["5"].kind == "open"


def test_indented_subbullet_not_counted() -> None:
    # The sub-bullet under task 4 must not become its own task.
    tasks = parse_tasks_md("phaseX", _SAMPLE)
    assert len(tasks) == 5


def test_deferred_beats_at_home() -> None:
    # Both markers present -> deferred wins.
    blob = "do the thing (At-home, runtime.) but actually (Deferred to pilot.)"
    assert classify_open(blob) == "deferred"


def test_lettered_ids() -> None:
    content = "- [ ] 8c. Session-trust tracking.\n- [x] 19a. Manual register. (author only)\n"
    tasks = {t.id: t for t in parse_tasks_md("p", content)}
    assert tasks["8c"].kind == "open"
    assert tasks["19a"].state == "done"


def test_rollup_orders_open_phases_first() -> None:
    tasks = [
        *parse_tasks_md("phase-done", "- [x] 1. done.\n"),
        *parse_tasks_md("phase-open", "- [ ] 1. open work.\n"),
    ]
    rollups = roll_up(tasks)
    # The phase with open work leads.
    assert rollups[0].phase == "phase-open"
    assert rollups[0].open_tasks and rollups[0].open_tasks[0].id == "1"
    assert rollups[1].phase == "phase-done"
    assert rollups[1].open_tasks == []
    assert rollups[1].done == 1


def test_parse_status_variants() -> None:
    assert parse_status("# Title\n\n**Status:** shipped\n") == "shipped"
    assert parse_status("# Title\n> Status: blocked\n") == "blocked"
    assert parse_status("# Title\nStatus = active\n") == "active"
    # Unknown word -> default active.
    assert parse_status("# Title\n**Status:** wibble\n") == "active"
    # Absent -> default active.
    assert parse_status("# Title\n\nsome prose\n") == "active"
    # Only scans the top of the file.
    tail = "# Title\n" + ("x\n" * 60) + "**Status:** shipped\n"
    assert parse_status(tail) == "active"


def test_rollup_collapses_shipped() -> None:
    tasks = [
        *parse_tasks_md("phase-shipped", "- [ ] 1. stale unticked box.\n- [x] 2. done.\n"),
        *parse_tasks_md("phase-active", "- [ ] 1. real open work.\n"),
    ]
    statuses = {"phase-shipped": "shipped", "phase-active": "active"}
    rollups = roll_up(tasks, statuses)  # type: ignore[arg-type]
    by_phase = {r.phase: r for r in rollups}

    shipped = by_phase["phase-shipped"]
    assert shipped.collapsed is True
    assert shipped.actionable_open == 0  # stale box doesn't count
    assert shipped.open_tasks  # still parsed, just not actionable

    active = by_phase["phase-active"]
    assert active.collapsed is False
    assert active.actionable_open == 1
    # Active-with-open leads the ordering over collapsed phases.
    assert rollups[0].phase == "phase-active"


def test_blocked_phase_is_actionable_but_flagged() -> None:
    tasks = parse_tasks_md("phase-blocked", "- [ ] 1. gated work.\n")
    rollups = roll_up(tasks, {"phase-blocked": "blocked"})  # type: ignore[arg-type]
    assert rollups[0].status == "blocked"
    assert rollups[0].collapsed is False
    assert rollups[0].actionable_open == 1

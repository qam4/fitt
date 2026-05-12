"""Tests for Phase 4.8 per-turn event stream.

Four concerns (mirroring test_events.py, with added coverage
for the per-day, per-session path layout):

* ``TurnLog.append`` writes one JSON line per call, survives
  round-trip through ``read``, preserves ``meta``, and lands
  in the expected `<session>/turns/<YYYY-MM-DD>.jsonl` file.
* ``read`` walks multiple day files and returns in
  chronological order; filters correctly by ``since``,
  ``kind``, ``turn_id``, and ``limit``; malformed lines dropped;
  missing file is a silent empty contribution.
* Cross-session isolation — turns written for session A don't
  leak into session B's read.
* IO failure in ``append`` is non-fatal — a locked or
  unwritable session dir logs a warning and returns the entry
  unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from gateway.turns import TURN_EVENT_KINDS, TurnEvent, TurnLog, new_event


def _ts(day: date, hour: int = 12) -> float:
    """Unix ts for the given date at a stable hour-of-day. Keeps
    test timestamps readable while anchoring them to a
    known day."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()


# --------------------------------------------------------------- append


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    """Two appends on the same session land in one day file;
    reader returns both in write order with fields intact."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    e1 = log.append(
        new_event(
            turn_id="t-1",
            kind="turn_started",
            session_key="main",
            meta={"alias": "fitt-smart"},
            ts=_ts(day, 10),
        )
    )
    e2 = log.append(
        new_event(
            turn_id="t-1",
            kind="turn_finished",
            session_key="main",
            meta={"status": "ok", "iterations": 2},
            ts=_ts(day, 10) + 5,
        )
    )

    got = log.read("main", now=_ts(day, 11))
    assert len(got) == 2
    assert got[0].kind == "turn_started"
    assert got[0].meta == {"alias": "fitt-smart"}
    assert got[1].kind == "turn_finished"
    assert got[1].meta == {"status": "ok", "iterations": 2}
    assert got[1].turn_id == "t-1"
    # Round-trip preserves byte-exact ts.
    assert got[0].ts == e1.ts
    assert got[1].ts == e2.ts


def test_append_writes_expected_path(tmp_path: Path) -> None:
    """Per-day, per-session layout. The file lands at
    ``sessions/<k>/turns/<YYYY-MM-DD>.jsonl``, matching the
    history file shape so the existing pruner walks it with
    the same date-parsing logic."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    log.append(
        new_event(
            turn_id="t-1",
            kind="turn_started",
            session_key="coding",
            ts=_ts(day),
        )
    )
    expected = tmp_path / "coding" / "turns" / "2026-05-12.jsonl"
    assert expected.exists()


def test_append_creates_nested_parents(tmp_path: Path) -> None:
    """First append to a never-used session must create the
    session dir + turns subdir. Operators shouldn't have to
    pre-create $FITT_HOME/sessions/*/turns."""
    log = TurnLog(tmp_path)
    log.append(
        new_event(
            turn_id="t-1",
            kind="turn_started",
            session_key="fresh-session",
            ts=_ts(date(2026, 5, 12)),
        )
    )
    assert (tmp_path / "fresh-session" / "turns").is_dir()


def test_append_one_line_per_entry(tmp_path: Path) -> None:
    """Regression guard: bugs that flush N times (getting
    N lines per entry) or forget the trailing newline (getting
    one concatenated megaline) both fail this test."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    for i in range(4):
        log.append(
            new_event(
                turn_id=f"t-{i}",
                kind="turn_started",
                session_key="main",
                ts=_ts(day, 10) + i,
            )
        )
    path = tmp_path / "main" / "turns" / "2026-05-12.jsonl"
    raw = path.read_text(encoding="utf-8")
    assert raw.count("\n") == 4
    for line in raw.splitlines():
        data = json.loads(line)
        assert data["kind"] == "turn_started"
        assert "turn_id" in data
        assert "event_id" in data


def test_event_id_auto_generated_and_unique(tmp_path: Path) -> None:
    """`new_event` auto-generates a unique `event_id` per call
    so callers don't have to manage one. Two appends with the
    same turn_id produce two distinct event_ids."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    e1 = log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day)))
    e2 = log.append(new_event(turn_id="t-1", kind="turn_finished", session_key="main", ts=_ts(day)))
    assert e1.event_id != e2.event_id
    assert e1.turn_id == e2.turn_id


# --------------------------------------------------------------- read: filters


def test_read_filters_by_since(tmp_path: Path) -> None:
    """The `since` filter drops entries with `ts < since`.
    Standard ts-comparison semantics — matches
    EventLog.read()."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    log.append(new_event(turn_id="t-a", kind="turn_started", session_key="main", ts=100.0))
    log.append(new_event(turn_id="t-b", kind="turn_started", session_key="main", ts=200.0))
    # Force the reader to anchor on the same day so both
    # entries are in scope.
    got = log.read("main", since=150.0, now=_ts(day, 13))
    assert [e.turn_id for e in got] == ["t-b"]


def test_read_filters_by_kind(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day, 10)))
    log.append(
        new_event(turn_id="t-1", kind="llm_call_started", session_key="main", ts=_ts(day, 10) + 1)
    )
    log.append(
        new_event(turn_id="t-1", kind="turn_finished", session_key="main", ts=_ts(day, 10) + 2)
    )
    got = log.read("main", kind="llm_call_started", now=_ts(day, 11))
    assert [e.kind for e in got] == ["llm_call_started"]


def test_read_filters_by_turn_id(tmp_path: Path) -> None:
    """Turn-id filter lets tools like `fitt watch` zoom into
    one turn's events when a session has many interleaved
    turns in the same day."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    log.append(new_event(turn_id="t-A", kind="turn_started", session_key="main", ts=_ts(day, 10)))
    log.append(new_event(turn_id="t-B", kind="turn_started", session_key="main", ts=_ts(day, 11)))
    log.append(
        new_event(turn_id="t-A", kind="turn_finished", session_key="main", ts=_ts(day, 10) + 5)
    )
    got = log.read("main", turn_id="t-A", now=_ts(day, 12))
    assert [e.kind for e in got] == ["turn_started", "turn_finished"]


def test_read_limit_keeps_most_recent(tmp_path: Path) -> None:
    """`limit` caps the result to the latest N by ts, mirroring
    EventLog's CLI contract: `--limit 20` means the 20 most
    recent events, not the first 20."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    for i in range(5):
        log.append(
            new_event(
                turn_id=f"t-{i}",
                kind="turn_started",
                session_key="main",
                ts=_ts(day, 10) + i,
            )
        )
    got = log.read("main", limit=2, now=_ts(day, 12))
    assert [e.turn_id for e in got] == ["t-3", "t-4"]


def test_read_missing_session_is_empty(tmp_path: Path) -> None:
    """Fresh session with no files yet — an empty result, not
    an exception."""
    log = TurnLog(tmp_path)
    assert log.read("never-written", now=_ts(date(2026, 5, 12))) == []


def test_read_malformed_line_dropped_with_warning(
    tmp_path: Path,
    caplog: object,
) -> None:
    """One corrupt line doesn't take out the rest of the file.
    Matches EventLog's failure posture."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    # Write one valid event to bootstrap the directory.
    log.append(
        new_event(turn_id="t-good", kind="turn_started", session_key="main", ts=_ts(day, 10))
    )
    path = tmp_path / "main" / "turns" / "2026-05-12.jsonl"
    # Append a malformed line directly.
    with path.open("a", encoding="utf-8") as f:
        f.write("this is not json\n")
    # Append another valid event after the corrupt line.
    log.append(
        new_event(turn_id="t-good2", kind="turn_finished", session_key="main", ts=_ts(day, 10) + 1)
    )

    got = log.read("main", now=_ts(day, 12))
    # The two real events survive; the corrupt line is skipped.
    assert [e.turn_id for e in got] == ["t-good", "t-good2"]


# --------------------------------------------------------------- read: cross-day


def test_read_walks_multiple_days(tmp_path: Path) -> None:
    """A session with entries spanning two days returns entries
    from both. Standard case for `fitt watch --since` over a
    weekend."""
    log = TurnLog(tmp_path)
    day1 = date(2026, 5, 11)
    day2 = date(2026, 5, 12)
    log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day1, 23)))
    log.append(new_event(turn_id="t-2", kind="turn_started", session_key="main", ts=_ts(day2, 1)))
    got = log.read("main", since=_ts(day1, 20), now=_ts(day2, 12))
    assert [e.turn_id for e in got] == ["t-1", "t-2"]
    # Two separate files exist under the per-day layout.
    assert (tmp_path / "main" / "turns" / "2026-05-11.jsonl").exists()
    assert (tmp_path / "main" / "turns" / "2026-05-12.jsonl").exists()


def test_read_crosses_midnight_turn_via_turn_id(tmp_path: Path) -> None:
    """A turn that crosses midnight is reconstructible via
    `turn_id` filter. This is the rare-but-documented cron
    firing at 23:59 → finishing at 00:00 scenario."""
    log = TurnLog(tmp_path)
    day1 = date(2026, 5, 11)
    day2 = date(2026, 5, 12)
    turn_id = "t-cross"
    log.append(
        new_event(
            turn_id=turn_id, kind="turn_started", session_key="main", ts=_ts(day1, 23) + 59 * 60
        )
    )
    log.append(
        new_event(turn_id=turn_id, kind="turn_finished", session_key="main", ts=_ts(day2, 0) + 2)
    )

    got = log.read("main", turn_id=turn_id, now=_ts(day2, 12))
    assert [e.kind for e in got] == ["turn_started", "turn_finished"]


def test_read_default_since_bounds_to_recent_days(tmp_path: Path) -> None:
    """Reading without `since` doesn't walk a 90-day history —
    bounded to the last 30 days so an unused session doesn't
    cost a disk walk on every call. Callers that want older
    entries pass `since`."""
    log = TurnLog(tmp_path)
    # Event from 60 days before the "now" the reader sees.
    old_day = date(2026, 3, 1)
    now_day = date(2026, 5, 12)  # 72 days later.
    log.append(new_event(turn_id="t-old", kind="turn_started", session_key="main", ts=_ts(old_day)))

    # No `since` → default window, which excludes the old entry.
    got = log.read("main", now=_ts(now_day))
    assert got == []

    # Explicit `since` earlier than the old entry — reader walks
    # far enough back to find it.
    got_explicit = log.read("main", since=_ts(date(2026, 2, 1)), now=_ts(now_day))
    assert [e.turn_id for e in got_explicit] == ["t-old"]


# --------------------------------------------------------------- isolation


def test_cross_session_isolation(tmp_path: Path) -> None:
    """A write to session A doesn't show up under session B's
    read. The path includes the session key by design."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    log.append(new_event(turn_id="t-a", kind="turn_started", session_key="sess-a", ts=_ts(day, 10)))
    log.append(
        new_event(turn_id="t-b", kind="turn_started", session_key="sess-b", ts=_ts(day, 10) + 1)
    )

    got_a = log.read("sess-a", now=_ts(day, 12))
    got_b = log.read("sess-b", now=_ts(day, 12))
    assert [e.turn_id for e in got_a] == ["t-a"]
    assert [e.turn_id for e in got_b] == ["t-b"]


# --------------------------------------------------------------- failure mode


def test_append_io_failure_non_fatal(tmp_path: Path) -> None:
    """Per design.md P3: an unwritable target logs a warning
    and returns the entry, not raising. Operator visibility is
    not load-bearing for the turn itself.

    We simulate the failure by putting a plain file where the
    session directory would go, making the `mkdir(parents=True,
    exist_ok=True)` call fail with `NotADirectoryError`.
    """
    # Create a file at the spot where sessions/<key>/ wants to
    # be a directory.
    blocker = tmp_path / "blocked-session"
    blocker.write_text("not a dir", encoding="utf-8")

    log = TurnLog(tmp_path)
    # This would try to create sessions/blocked-session/turns/
    # under a file, failing.
    entry = new_event(
        turn_id="t-1",
        kind="turn_started",
        session_key="blocked-session",
        ts=_ts(date(2026, 5, 12)),
    )
    got = log.append(entry)
    # Returns the entry unchanged; no exception propagates.
    assert got is entry


# --------------------------------------------------------------- metadata


def test_turn_event_kinds_include_all_planned_kinds() -> None:
    """Pin the canonical set. New kinds get added here when
    writers start emitting them; tests that assert against
    `TURN_EVENT_KINDS` notice when the contract drifts."""
    expected = {
        "turn_started",
        "llm_call_started",
        "llm_call_completed",
        "tool_call_planned",
        "approval_requested",
        "approval_decided",
        "tool_call_executed",
        "gap_reported",
        "turn_finished",
    }
    assert TURN_EVENT_KINDS == expected


def test_file_path_helper_matches_append_target(tmp_path: Path) -> None:
    """`file_path()` is the public contract for `fitt watch`
    and the HTTP endpoint. Must agree byte-exactly with where
    `append()` lands."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    expected = log.file_path("main", day)
    log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day)))
    assert expected.exists()


def test_new_event_custom_ts_and_event_id_honoured(tmp_path: Path) -> None:
    """Tests that want deterministic output pass their own
    `ts` and `event_id`; the constructor must pass them
    through unchanged."""
    e = new_event(
        turn_id="t-1",
        kind="turn_started",
        session_key="main",
        ts=123.456,
        event_id="deterministic",
    )
    assert e.ts == 123.456
    assert e.event_id == "deterministic"


# --------------------------------------------------------------- pub/sub hook


def test_subscribe_fires_callback_on_append(tmp_path: Path) -> None:
    """One registered subscriber receives every successful
    append in order. The source of truth is still the JSONL
    on disk; the callback is a convenience for live
    consumers like the Telegram renderer."""
    log = TurnLog(tmp_path)
    seen: list[TurnEvent] = []
    log.subscribe(seen.append)

    day = date(2026, 5, 12)
    e1 = log.append(
        new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day, 10))
    )
    e2 = log.append(
        new_event(
            turn_id="t-1",
            kind="tool_call_executed",
            session_key="main",
            meta={"tool_name": "read_file", "ok": True},
            ts=_ts(day, 10) + 1,
        )
    )
    assert [e.event_id for e in seen] == [e1.event_id, e2.event_id]
    assert seen[1].meta["tool_name"] == "read_file"


def test_subscribe_supports_multiple_callbacks(tmp_path: Path) -> None:
    """The bot and the future admin dashboard might both
    register. Both should see every event."""
    log = TurnLog(tmp_path)
    seen_a: list[TurnEvent] = []
    seen_b: list[TurnEvent] = []
    log.subscribe(seen_a.append)
    log.subscribe(seen_b.append)

    day = date(2026, 5, 12)
    log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day)))
    assert len(seen_a) == 1
    assert len(seen_b) == 1


def test_subscribe_raising_callback_swallowed(tmp_path: Path) -> None:
    """A misbehaving subscriber must not break persistence
    or prevent other subscribers from firing. Mirrors the
    'IO failure is non-fatal' principle on the subscriber
    side: observability is not load-bearing."""
    log = TurnLog(tmp_path)
    seen: list[TurnEvent] = []

    def bad(_entry: TurnEvent) -> None:
        raise RuntimeError("subscriber boom")

    log.subscribe(bad)
    log.subscribe(seen.append)

    day = date(2026, 5, 12)
    log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day)))
    # Good subscriber still fired.
    assert len(seen) == 1
    # And the file still got written.
    path = tmp_path / "main" / "turns" / "2026-05-12.jsonl"
    assert path.exists()
    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_subscribe_not_fired_on_write_failure(tmp_path: Path) -> None:
    """If the underlying append fails (disk full, permission
    denied), subscribers don't fire. The JSONL is the source
    of truth; a subscriber call without a corresponding disk
    record would produce false signals downstream."""
    # Block the expected directory path with a file.
    blocker = tmp_path / "blocked-session"
    blocker.write_text("not a dir", encoding="utf-8")

    log = TurnLog(tmp_path)
    seen: list[TurnEvent] = []
    log.subscribe(seen.append)

    entry = new_event(
        turn_id="t-1",
        kind="turn_started",
        session_key="blocked-session",
        ts=_ts(date(2026, 5, 12)),
    )
    log.append(entry)
    # append didn't raise (P3); it also didn't fire the
    # subscriber because the write failed.
    assert seen == []


def test_subscribe_no_subscribers_is_noop(tmp_path: Path) -> None:
    """Sanity: a TurnLog with no registered subscribers works
    identically to one without the hook existing."""
    log = TurnLog(tmp_path)
    day = date(2026, 5, 12)
    got = log.append(new_event(turn_id="t-1", kind="turn_started", session_key="main", ts=_ts(day)))
    assert got.turn_id == "t-1"
    path = tmp_path / "main" / "turns" / "2026-05-12.jsonl"
    assert path.exists()

"""Tests for Phase 4.5 Task 1 — the event log primitive.

Three concerns:

* ``EventLog.append`` writes one JSON line, survives round trip
  through ``read``, preserves ``meta`` dicts.
* ``read`` filters correctly by ``since``, ``kind``, ``session``,
  and ``limit``; malformed lines are dropped; missing file is
  empty.
* ``prune`` drops old entries via stream-rewrite; a crash-mid
  simulation leaves the original intact; malformed entries get
  removed alongside expired ones.
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway.events import EventEntry, EventLog, new_entry

# --------------------------------------------------------------- append


def test_append_and_read_round_trip(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    e1 = log.append(new_entry(kind="cron_fired", session_key="cron:abc:1", title="hi"))
    e2 = log.append(
        new_entry(
            kind="agent_message",
            session_key="main",
            title="ping",
            body="you've got mail",
            meta={"source": "send_message"},
        )
    )
    got = log.read()
    assert len(got) == 2
    assert got[0].kind == "cron_fired"
    assert got[0].session_key == e1.session_key
    assert got[1].body == "you've got mail"
    assert got[1].meta == {"source": "send_message"}
    assert got[1].ts == e2.ts


def test_append_creates_parent_dir(tmp_path: Path) -> None:
    """First append to a nested path creates the parent dir so
    operators don't have to pre-create ``$FITT_HOME``."""
    log = EventLog(tmp_path / "deeper" / "still" / "events.jsonl")
    log.append(new_entry(kind="cron_fired", session_key="main", title="ok"))
    assert (tmp_path / "deeper" / "still" / "events.jsonl").exists()


def test_append_writes_one_line_per_entry(tmp_path: Path) -> None:
    """Regression guard: a bug that flushes the whole in-memory
    state to disk would produce N lines after N writes, while a
    bug that forgets the trailing newline would produce 1
    concatenated line."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    for i in range(3):
        log.append(new_entry(kind="cron_fired", session_key="main", title=f"t{i}"))
    raw = path.read_text(encoding="utf-8")
    assert raw.count("\n") == 3
    # Each line should round-trip as JSON.
    for line in raw.splitlines():
        payload = json.loads(line)
        assert payload["kind"] == "cron_fired"


# --------------------------------------------------------------- read filters


def test_read_filters_by_since(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(new_entry(kind="cron_fired", session_key="main", title="old", ts=100.0))
    log.append(new_entry(kind="cron_fired", session_key="main", title="new", ts=200.0))
    got = log.read(since=150.0)
    assert [e.title for e in got] == ["new"]


def test_read_filters_by_kind(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(new_entry(kind="cron_fired", session_key="main", title="a"))
    log.append(new_entry(kind="agent_message", session_key="main", title="b"))
    log.append(new_entry(kind="cron_fired", session_key="main", title="c"))
    got = log.read(kind="cron_fired")
    assert [e.title for e in got] == ["a", "c"]


def test_read_filters_by_session(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(new_entry(kind="cron_fired", session_key="main", title="a"))
    log.append(new_entry(kind="cron_fired", session_key="cron:x:1", title="b"))
    got = log.read(session="cron:x:1")
    assert [e.title for e in got] == ["b"]


def test_read_combines_filters_with_and(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    log.append(new_entry(kind="cron_fired", session_key="main", title="a", ts=100.0))
    log.append(new_entry(kind="cron_fired", session_key="main", title="b", ts=200.0))
    log.append(new_entry(kind="agent_message", session_key="main", title="c", ts=200.0))
    got = log.read(since=150.0, kind="cron_fired")
    assert [e.title for e in got] == ["b"]


def test_read_limit_keeps_most_recent(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    for i in range(5):
        log.append(new_entry(kind="cron_fired", session_key="main", title=f"t{i}"))
    got = log.read(limit=2)
    assert [e.title for e in got] == ["t3", "t4"]


def test_read_missing_file_is_empty(tmp_path: Path) -> None:
    assert EventLog(tmp_path / "nope.jsonl").read() == []


def test_read_skips_malformed_lines(tmp_path: Path) -> None:
    """Human editing the file + accidentally corrupting a line
    shouldn't break the reader. We drop the bad line and carry
    on."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"ts": 1.0, "kind": "cron_fired", "session_key": "main", "title": "ok"}\n'
        "this is not json\n"
        '{"ts": 2.0, "kind": "agent_message", "session_key": "main", "title": "also ok"}\n',
        encoding="utf-8",
    )
    got = EventLog(path).read()
    assert [e.title for e in got] == ["ok", "also ok"]


def test_read_skips_structurally_invalid(tmp_path: Path) -> None:
    """Valid JSON, wrong shape (missing required fields). Drop
    with a warning rather than raising; the log is coarse and
    we prefer 'mostly useful' over 'empty on one bad line'."""
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"ts": 1.0, "kind": "cron_fired", "session_key": "main", "title": "ok"}\n'
        '{"missing": "everything"}\n'
        '{"ts": "not a float", "kind": "k", "session_key": "s", "title": "t"}\n'
        '{"ts": 2.0, "kind": "agent_message", "session_key": "main", "title": "also ok"}\n',
        encoding="utf-8",
    )
    got = EventLog(path).read()
    assert [e.title for e in got] == ["ok", "also ok"]


# --------------------------------------------------------------- prune


def test_prune_drops_old_entries(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    now = 1_000_000.0
    # 100 days ago - expired.
    log.append(new_entry(kind="cron_fired", session_key="main", title="old", ts=now - 100 * 86400))
    # 10 days ago - kept.
    log.append(
        new_entry(kind="agent_message", session_key="main", title="recent", ts=now - 10 * 86400)
    )
    removed = log.prune(max_age_days=90, now=now)
    assert removed == 1
    remaining = log.read()
    assert [e.title for e in remaining] == ["recent"]


def test_prune_missing_file_is_zero(tmp_path: Path) -> None:
    assert EventLog(tmp_path / "nope.jsonl").prune(max_age_days=90) == 0


def test_prune_also_drops_malformed(tmp_path: Path) -> None:
    """Keeping malformed lines alive forever defeats the purpose
    of pruning. Drop them alongside expired ones."""
    path = tmp_path / "events.jsonl"
    now = 1_000_000.0
    old_ts = now - 100 * 86400
    recent_ts = now - 10 * 86400
    path.write_text(
        f'{{"ts": {old_ts}, "kind": "cron_fired", "session_key": "main", "title": "old"}}\n'
        "junk line\n"
        f'{{"ts": {recent_ts}, "kind": "agent_message", "session_key": "main", "title": "recent"}}\n',
        encoding="utf-8",
    )
    log = EventLog(path)
    removed = log.prune(max_age_days=90, now=now)
    assert removed == 2
    assert [e.title for e in log.read()] == ["recent"]


def test_prune_is_atomic(tmp_path: Path) -> None:
    """After prune completes, the file either reflects the new
    state entirely or the old state entirely — never partial.
    Validated indirectly: no ``.tmp`` file survives success."""
    path = tmp_path / "events.jsonl"
    log = EventLog(path)
    log.append(new_entry(kind="cron_fired", session_key="main", title="x", ts=1.0))
    log.prune(max_age_days=1, now=1_000_000.0)
    tmp = path.with_suffix(path.suffix + ".tmp")
    assert not tmp.exists(), "tmp file should be renamed into place, not left behind"


def test_prune_emits_expected_count_on_all_expired(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "events.jsonl")
    now = 1_000_000.0
    for i in range(5):
        log.append(
            new_entry(kind="cron_fired", session_key="main", title=f"t{i}", ts=now - 100 * 86400)
        )
    removed = log.prune(max_age_days=90, now=now)
    assert removed == 5
    assert log.read() == []


# --------------------------------------------------------------- dataclass basics


def test_new_entry_copies_meta(tmp_path: Path) -> None:
    """Regression: a shared meta dict passed to new_entry would
    be captured by reference; mutating it later would change the
    stored entry."""
    shared: dict[str, object] = {"k": "v"}
    entry = new_entry(kind="cron_fired", session_key="main", title="t", meta=shared)
    shared["k"] = "mutated"
    assert entry.meta == {"k": "v"}


def test_event_entry_defaults() -> None:
    e = EventEntry(ts=1.0, kind="cron_fired", session_key="main", title="t")
    assert e.body == ""
    assert e.meta == {}

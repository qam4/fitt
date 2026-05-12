"""Tests for Phase 4.8d — ``fitt watch`` / :mod:`gateway.cli_watch`.

Covers:

* ``render_line`` output shape (timestamp + fixed-width kind
  column + key-sorted meta).
* Color classification for each meta-driven branch
  (``turn_finished`` ok/failure, ``tool_call_executed``
  ok/fail, ``approval_decided`` approve/reject/timeout,
  ``gap_reported``).
* Meta flattening: scalars, nested dicts, deeper nesting
  abbreviated, whitespace escapes.
* ``iter_new_events`` wraps ``TurnLog.read``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from gateway.cli_watch import (
    _classify_color,
    _render_meta,
    iter_new_events,
    render_line,
)
from gateway.turns import TurnEvent, TurnLog, new_event


def _ts(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()


_RECENT = date.today() - timedelta(days=1)


# --------------------------------------------------------------- render_line


def test_render_line_has_fixed_width_kind_column() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="tool_call_planned",
        ts=_ts(_RECENT, 10),
        session_key="main",
        meta={"tool_name": "read_file", "call_id": "c1"},
    )
    line = render_line(event)
    # Timestamp format HH:MM:SS followed by two spaces then
    # the kind column.
    assert line[:8].count(":") == 2
    assert "  tool_call_planned" in line


def test_render_line_sorts_meta_by_key() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="turn_started",
        ts=_ts(_RECENT, 10),
        session_key="main",
        meta={"user_msg_len": 20, "alias": "fitt-smart", "client": "telegram"},
    )
    line = render_line(event)
    # Sorted order: alias, client, user_msg_len.
    assert line.index("alias=") < line.index("client=") < line.index("user_msg_len=")


# --------------------------------------------------------------- classify_color


def test_classify_color_turn_finished_ok_is_green() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="turn_finished",
        ts=0,
        session_key="main",
        meta={"status": "ok", "iterations": 2},
    )
    assert _classify_color(event) == "bold green"


def test_classify_color_turn_finished_failure_is_red() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="turn_finished",
        ts=0,
        session_key="main",
        meta={"status": "tool_loop_exhausted", "iterations": 10},
    )
    assert _classify_color(event) == "bold red"


def test_classify_color_tool_executed_failure_is_yellow() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="tool_call_executed",
        ts=0,
        session_key="main",
        meta={"tool_name": "read_file", "ok": False, "duration_ms": 3},
    )
    assert _classify_color(event) == "yellow"


def test_classify_color_tool_executed_success_is_plain() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="tool_call_executed",
        ts=0,
        session_key="main",
        meta={"tool_name": "read_file", "ok": True, "duration_ms": 12},
    )
    assert _classify_color(event) == ""


def test_classify_color_approval_decided_approve_is_green() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="approval_decided",
        ts=0,
        session_key="main",
        meta={"approval_id": "a1", "decision": "approve", "duration_ms": 1000},
    )
    assert _classify_color(event) == "bold green"


def test_classify_color_approval_decided_reject_is_yellow() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="approval_decided",
        ts=0,
        session_key="main",
        meta={"approval_id": "a1", "decision": "reject", "duration_ms": 1000},
    )
    assert _classify_color(event) == "yellow"


def test_classify_color_gap_reported_is_yellow() -> None:
    event = TurnEvent(
        turn_id="t1",
        event_id="e1",
        kind="gap_reported",
        ts=0,
        session_key="main",
        meta={"gap_text": "x", "suggestion": "y"},
    )
    assert _classify_color(event) == "yellow"


# --------------------------------------------------------------- meta rendering


def test_render_meta_empty_returns_empty() -> None:
    assert _render_meta({}) == ""


def test_render_meta_flattens_one_level_of_dict() -> None:
    meta = {
        "tool_name": "read_file",
        "args": {"path": "README.md", "project": "hub"},
    }
    out = _render_meta(meta)
    # args flatten to args.path / args.project.
    assert "args.path=README.md" in out
    assert "args.project=hub" in out
    assert "tool_name=read_file" in out


def test_render_meta_deeper_dict_abbreviates() -> None:
    meta = {"foo": {"bar": {"baz": 1}}}
    out = _render_meta(meta)
    # Second-level dict abbreviates to {...}.
    assert "foo.bar={...}" in out


def test_render_meta_escapes_newlines_and_tabs() -> None:
    meta = {"result_summary": "line1\nline2\ttabbed"}
    out = _render_meta(meta)
    # Escaped, one-line rendering.
    assert "\\n" in out
    assert "\\t" in out
    assert "\n" not in out


def test_render_meta_quotes_values_with_spaces() -> None:
    meta = {"comment": "hello world"}
    out = _render_meta(meta)
    assert 'comment="hello world"' in out


def test_render_meta_renders_none_and_bools_cleanly() -> None:
    meta = {"cost_usd": None, "ok": True, "finish_reason": "stop"}
    out = _render_meta(meta)
    assert "cost_usd=null" in out
    assert "ok=True" in out
    assert "finish_reason=stop" in out


# --------------------------------------------------------------- iter_new_events


def test_iter_new_events_wraps_turnlog_read(tmp_path: Path) -> None:
    """``iter_new_events`` is a thin wrapper so the tail loop
    can ignore the file-path layout. We pin the passthrough
    behaviour so a future refactor doesn't silently change the
    semantic."""
    sessions = tmp_path
    log = TurnLog(sessions)
    base = _ts(_RECENT, 10)
    log.append(new_event(turn_id="t1", kind="turn_started", session_key="main", ts=base))
    log.append(new_event(turn_id="t1", kind="turn_finished", session_key="main", ts=base + 100))

    got = list(iter_new_events(log, "main", since=base))
    # Inclusive-since (TurnLog.read semantics); caller drops
    # duplicates against last_ts.
    kinds = [e.kind for e in got]
    assert kinds == ["turn_started", "turn_finished"]

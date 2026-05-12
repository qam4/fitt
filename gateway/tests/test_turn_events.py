"""Tests for the Phase 4.8a emission helpers.

One test per helper confirming the event's kind and meta
shape against design.md's schema, plus the two no-op
branches (``turns=None`` and ``turn_id=None``) and the
swallow-on-error branch so a broken log doesn't kill the
agent loop.

These tests use a real :class:`TurnLog` over a tmp dir so
the helper-to-log round trip is exercised end-to-end, not
mocked. If the helper's ``meta`` shape drifts from what
the renderer expects, the test breaks here — which is the
point.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gateway.turn_events import (
    record_approval_decided,
    record_approval_requested,
    record_gap_event,
    record_llm_call_completed,
    record_llm_call_started,
    record_tool_call_executed,
    record_tool_call_planned,
    record_turn_finished,
    record_turn_started,
)
from gateway.turns import TurnEvent, TurnLog


def _ts(day: date, hour: int = 12) -> float:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()


def _one_event(log: TurnLog, session_key: str) -> TurnEvent:
    got = log.read(session_key, now=_ts(date(2026, 5, 12), 23))
    assert len(got) == 1, f"expected exactly one event, got {got}"
    return got[0]


# --------------------------------------------------------------- turn lifecycle


def test_record_turn_started(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_turn_started(
        log,
        "turn-1",
        "main",
        alias="fitt-smart",
        client="telegram",
        user_msg_len=42,
    )
    e = _one_event(log, "main")
    assert e.kind == "turn_started"
    assert e.turn_id == "turn-1"
    assert e.meta == {
        "alias": "fitt-smart",
        "client": "telegram",
        "user_msg_len": 42,
    }


def test_record_turn_finished(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_turn_finished(
        log,
        "turn-1",
        "main",
        status="ok",
        iterations=3,
        final_reply_len=412,
    )
    e = _one_event(log, "main")
    assert e.kind == "turn_finished"
    assert e.meta == {"status": "ok", "iterations": 3, "final_reply_len": 412}


def test_record_turn_finished_with_error_status(tmp_path: Path) -> None:
    """Error statuses match AgentLoopResult.status values
    byte-for-byte — subscribers branch on the string, so
    a drift here breaks the renderer."""
    log = TurnLog(tmp_path)
    record_turn_finished(
        log,
        "turn-1",
        "main",
        status="tool_loop_exhausted",
        iterations=10,
        final_reply_len=0,
    )
    e = _one_event(log, "main")
    assert e.meta["status"] == "tool_loop_exhausted"


# --------------------------------------------------------------- llm


def test_record_llm_call_started(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_llm_call_started(log, "turn-1", "main", alias="fitt-smart", iteration=1)
    e = _one_event(log, "main")
    assert e.kind == "llm_call_started"
    assert e.meta == {"alias": "fitt-smart", "iteration": 1}


def test_record_llm_call_completed(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_llm_call_completed(
        log,
        "turn-1",
        "main",
        model="deepseek-v4-flash",
        latency_ms=920,
        in_tokens=412,
        out_tokens=35,
        finish_reason="tool_calls",
        tool_calls_count=1,
        cost_usd=Decimal("0.00123"),
    )
    e = _one_event(log, "main")
    assert e.kind == "llm_call_completed"
    assert e.meta["model"] == "deepseek-v4-flash"
    assert e.meta["latency_ms"] == 920
    assert e.meta["in_tokens"] == 412
    assert e.meta["out_tokens"] == 35
    assert e.meta["finish_reason"] == "tool_calls"
    assert e.meta["tool_calls_count"] == 1
    # Decimal converts to float at the schema boundary per the
    # helper's contract. Precision loss is acceptable; audit.jsonl
    # carries the full-precision cost.
    assert isinstance(e.meta["cost_usd"], float)
    assert e.meta["cost_usd"] == pytest.approx(0.00123)


def test_record_llm_call_completed_with_none_cost_omits_field(
    tmp_path: Path,
) -> None:
    """Local Ollama and no-cost models produce cost_usd=None —
    the helper drops the field rather than emitting a
    literal null that subscribers then need to special-case."""
    log = TurnLog(tmp_path)
    record_llm_call_completed(
        log,
        "turn-1",
        "main",
        model="qwen2.5-coder:14b",
        latency_ms=1200,
        in_tokens=100,
        out_tokens=50,
        finish_reason="stop",
        tool_calls_count=0,
        cost_usd=None,
    )
    e = _one_event(log, "main")
    assert "cost_usd" not in e.meta


# --------------------------------------------------------------- tool calls


def test_record_tool_call_planned(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    args = {"project": "hub", "path": "README.md"}
    record_tool_call_planned(
        log,
        "turn-1",
        "main",
        tool_name="read_file",
        args=args,
        call_id="c1",
        iteration=1,
    )
    e = _one_event(log, "main")
    assert e.kind == "tool_call_planned"
    assert e.meta == {
        "tool_name": "read_file",
        "args": args,
        "call_id": "c1",
        "iteration": 1,
    }


def test_record_tool_call_executed_success(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_tool_call_executed(
        log,
        "turn-1",
        "main",
        tool_name="read_file",
        call_id="c1",
        ok=True,
        duration_ms=15,
        result_summary="wrote 1KB to context",
    )
    e = _one_event(log, "main")
    assert e.kind == "tool_call_executed"
    assert e.meta == {
        "tool_name": "read_file",
        "call_id": "c1",
        "ok": True,
        "duration_ms": 15,
        "result_summary": "wrote 1KB to context",
    }


def test_record_tool_call_executed_with_artifact(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_tool_call_executed(
        log,
        "turn-1",
        "main",
        tool_name="project_shell",
        call_id="c1",
        ok=True,
        duration_ms=340,
        result_summary="exit=0, 12000 bytes of output",
        artifact_path="/tmp/fitt/artifacts/shell-abc.txt",
        exit_code=0,
    )
    e = _one_event(log, "main")
    assert e.meta["artifact_path"] == "/tmp/fitt/artifacts/shell-abc.txt"
    assert e.meta["exit_code"] == 0


def test_record_tool_call_executed_result_summary_capped(tmp_path: Path) -> None:
    """300-char cap on result_summary so a leaky tool can't
    bloat every turn event with a full dump."""
    log = TurnLog(tmp_path)
    long_summary = "x" * 500
    record_tool_call_executed(
        log,
        "turn-1",
        "main",
        tool_name="grep_repo",
        call_id="c1",
        ok=True,
        duration_ms=100,
        result_summary=long_summary,
    )
    e = _one_event(log, "main")
    # 297 chars + "..." = 300 total.
    assert len(e.meta["result_summary"]) == 300
    assert e.meta["result_summary"].endswith("...")


def test_record_tool_call_executed_failure(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_tool_call_executed(
        log,
        "turn-1",
        "main",
        tool_name="read_file",
        call_id="c1",
        ok=False,
        duration_ms=8,
        result_summary="file not found",
    )
    e = _one_event(log, "main")
    assert e.meta["ok"] is False
    assert e.meta["result_summary"] == "file not found"


# --------------------------------------------------------------- approval


def test_record_approval_requested(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_approval_requested(
        log,
        "turn-1",
        "main",
        approval_id="ap-42",
        tool_name="edit_file",
        bucket="ask",
        client="telegram",
    )
    e = _one_event(log, "main")
    assert e.kind == "approval_requested"
    assert e.meta == {
        "approval_id": "ap-42",
        "tool_name": "edit_file",
        "bucket": "ask",
        "client": "telegram",
    }


def test_record_approval_decided(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_approval_decided(
        log,
        "turn-1",
        "main",
        approval_id="ap-42",
        decision="approve",
        duration_ms=3200,
    )
    e = _one_event(log, "main")
    assert e.kind == "approval_decided"
    assert e.meta == {
        "approval_id": "ap-42",
        "decision": "approve",
        "duration_ms": 3200,
    }


def test_record_approval_decided_timeout(tmp_path: Path) -> None:
    """The wait-timeout branch in ApprovalMiddleware ends up
    here too — the renderer needs to know whether the user
    decided or the approval expired."""
    log = TurnLog(tmp_path)
    record_approval_decided(
        log,
        "turn-1",
        "main",
        approval_id="ap-42",
        decision="timeout",
        duration_ms=45000,
    )
    e = _one_event(log, "main")
    assert e.meta["decision"] == "timeout"


# --------------------------------------------------------------- gap


def test_record_gap_event(tmp_path: Path) -> None:
    log = TurnLog(tmp_path)
    record_gap_event(
        log,
        "turn-1",
        "main",
        gap_text="I'd need a tool to search the web.",
        suggestion="adding web_search",
    )
    e = _one_event(log, "main")
    assert e.kind == "gap_reported"
    assert e.meta == {
        "gap_text": "I'd need a tool to search the web.",
        "suggestion": "adding web_search",
    }


# --------------------------------------------------------------- no-op branches


def test_none_log_is_noop() -> None:
    """Passing ``turns=None`` must not raise. Supports the
    contract that observability is optional."""
    record_turn_started(None, "turn-1", "main", alias="x", client="y", user_msg_len=1)
    record_turn_finished(None, "turn-1", "main", status="ok", iterations=1, final_reply_len=1)
    record_llm_call_started(None, "turn-1", "main", alias="x", iteration=1)
    record_llm_call_completed(
        None,
        "turn-1",
        "main",
        model="m",
        latency_ms=1,
        in_tokens=1,
        out_tokens=1,
        finish_reason="stop",
        tool_calls_count=0,
        cost_usd=None,
    )
    record_tool_call_planned(
        None, "turn-1", "main", tool_name="t", args={}, call_id="c", iteration=1
    )
    record_tool_call_executed(
        None,
        "turn-1",
        "main",
        tool_name="t",
        call_id="c",
        ok=True,
        duration_ms=1,
        result_summary="ok",
    )
    record_approval_requested(
        None,
        "turn-1",
        "main",
        approval_id="a",
        tool_name="t",
        bucket="ask",
        client="telegram",
    )
    record_approval_decided(
        None, "turn-1", "main", approval_id="a", decision="approve", duration_ms=1
    )
    record_gap_event(None, "turn-1", "main", gap_text="x", suggestion="y")


def test_none_turn_id_is_noop(tmp_path: Path) -> None:
    """``turn_id=None`` short-circuits. Supports early-phase
    callers that don't yet generate turn ids — they can
    thread None through without changing the helper's
    signature."""
    log = TurnLog(tmp_path)
    record_turn_started(log, None, "main", alias="x", client="y", user_msg_len=1)
    # No events written.
    assert log.read("main", now=_ts(date(2026, 5, 12), 23)) == []


# --------------------------------------------------------------- swallow errors


@dataclass
class _RaisingLog:
    """Stand-in for TurnLog that raises on append. Used to
    pin the swallow-on-error branch without monkey-patching
    the real TurnLog class."""

    def append(self, entry: Any) -> None:
        raise RuntimeError("boom from append")


def test_emit_swallows_append_errors(caplog: pytest.LogCaptureFixture) -> None:
    """A failing TurnLog.append must not propagate out of
    the helper — the agent loop must never die on a
    turn-event write failure."""
    bad_log = _RaisingLog()
    record_turn_started(
        bad_log,  # type: ignore[arg-type]
        "turn-1",
        "main",
        alias="x",
        client="y",
        user_msg_len=1,
    )
    # No exception propagated.
    # A warning was logged with the failure context.
    assert any("turn_events.emit_failed" in r.message for r in caplog.records)

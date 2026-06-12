"""Tests for Phase 12 task 13 — the ground-truth trouble detector.

Pure logic, no model. Each ground-truth signal is exercised on a
synthetic transcript; precedence between co-occurring signals is
pinned; and a hypothesis property asserts the C4-negative invariant:
recovery never fires on a clean, all-success transcript.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.memory import PersistedToolCall
from gateway.trouble import NO_TROUBLE, Trouble, detect_trouble


def _call(
    name: str = "noop",
    *,
    ok: bool = True,
    args: dict[str, Any] | None = None,
    status: str | None = None,
) -> PersistedToolCall:
    return PersistedToolCall(
        tool_name=name,
        args=args if args is not None else {},
        result_status=status if status is not None else ("ok" if ok else "error"),
        result_summary="" if ok else "boom",
    )


# --------------------------------------------------------------- clean


def test_clean_transcript_is_no_trouble() -> None:
    t = detect_trouble(
        status="ok",
        tool_calls=[_call("read_file", args={"p": "a"}), _call("grep", args={"q": "x"})],
        assistant_text="here is your answer",
    )
    assert t == NO_TROUBLE
    assert t.kind == "none"
    assert bool(t) is False


def test_no_tools_non_empty_reply_is_clean() -> None:
    t = detect_trouble(status="ok", tool_calls=[], assistant_text="42")
    assert t.kind == "none"


# --------------------------------------------------------------- each signal


def test_empty_after_tools() -> None:
    t = detect_trouble(status="ok", tool_calls=[_call()], assistant_text="   ")
    assert t.kind == "empty_after_tools"
    assert bool(t) is True


def test_empty_reply_without_tools_is_not_empty_after_tools() -> None:
    # No tools ran, so the empty-after-tools signal must not fire.
    t = detect_trouble(status="ok", tool_calls=[], assistant_text="")
    assert t.kind == "none"


def test_identical_failing_retry() -> None:
    call = _call("http_get", ok=False, args={"url": "x"})
    t = detect_trouble(status="ok", tool_calls=[call, call], assistant_text="trying")
    assert t.kind == "identical_retry"


def test_identical_retry_that_finally_succeeds_is_not_trouble() -> None:
    # Same name+args, but the latest attempt succeeded -> recovery
    # working, not a doom loop.
    failed = _call("http_get", ok=False, args={"url": "x"})
    ok = _call("http_get", ok=True, args={"url": "x"})
    t = detect_trouble(status="ok", tool_calls=[failed, ok], assistant_text="got it")
    assert t.kind == "none"


def test_identical_args_different_tool_is_not_retry() -> None:
    a = _call("read_file", ok=False, args={"p": "x"})
    b = _call("grep", ok=False, args={"p": "x"})
    # Falls through identical_retry; the last errored with only 2
    # calls (< threshold) -> tool_error.
    t = detect_trouble(status="ok", tool_calls=[a, b], assistant_text="hmm")
    assert t.kind == "tool_error"


def test_zero_progress() -> None:
    calls = [
        _call("a", ok=False, args={"i": 1}),
        _call("b", ok=False, args={"i": 2}),
        _call("c", ok=False, args={"i": 3}),
    ]
    t = detect_trouble(status="ok", tool_calls=calls, assistant_text="still working")
    assert t.kind == "zero_progress"


def test_zero_progress_threshold_not_reached_is_tool_error() -> None:
    calls = [_call("a", ok=False, args={"i": 1}), _call("b", ok=False, args={"i": 2})]
    t = detect_trouble(
        status="ok",
        tool_calls=calls,
        assistant_text="hmm",
        zero_progress_threshold=3,
    )
    assert t.kind == "tool_error"


def test_tool_error_single_call() -> None:
    t = detect_trouble(
        status="ok",
        tool_calls=[_call("write_file", ok=False, args={"p": "x"})],
        assistant_text="that failed",
    )
    assert t.kind == "tool_error"
    assert "write_file" in t.detail


def test_tool_error_uses_shell_exit_status() -> None:
    call = _call("project_shell", args={"cmd": "false"}, status="exit=1")
    t = detect_trouble(status="ok", tool_calls=[call], assistant_text="nonzero")
    assert t.kind == "tool_error"
    assert "exit=1" in t.detail


def test_budget_exhausted_when_no_specific_cause() -> None:
    # Exhausted, but every call succeeded — the generic catch-all.
    calls = [_call("a", args={"i": 1}), _call("b", args={"i": 2}), _call("c", args={"i": 3})]
    t = detect_trouble(status="tool_loop_exhausted", tool_calls=calls, assistant_text="")
    assert t.kind == "budget_exhausted"


# --------------------------------------------------------------- precedence


def test_zero_progress_beats_tool_error() -> None:
    calls = [_call("a", ok=False, args={"i": i}) for i in range(3)]
    t = detect_trouble(status="ok", tool_calls=calls, assistant_text="x")
    assert t.kind == "zero_progress"  # not tool_error


def test_identical_retry_beats_zero_progress() -> None:
    bad = _call("loop", ok=False, args={"n": 1})
    calls = [_call("warmup", ok=False, args={"i": 0}), bad, bad]
    t = detect_trouble(status="ok", tool_calls=calls, assistant_text="x")
    assert t.kind == "identical_retry"  # caught before the zero-progress sweep


def test_specific_cause_beats_budget_exhausted() -> None:
    # Exhausted AND the last call errored -> report the actionable
    # tool_error, not the generic budget signal.
    calls = [_call("a", args={"i": 1}), _call("b", ok=False, args={"i": 2})]
    t = detect_trouble(status="tool_loop_exhausted", tool_calls=calls, assistant_text="")
    assert t.kind == "tool_error"


def test_upstream_error_status_is_out_of_scope() -> None:
    # No tool-level fact, dispatch-layer error -> recovery doesn't own
    # this; returns none.
    t = detect_trouble(status="upstream_error", tool_calls=[], assistant_text="")
    assert t.kind == "none"


# --------------------------------------------------------------- property C4 (negative)


@settings(max_examples=200)
@given(
    names=st.lists(st.text(min_size=1, max_size=8), min_size=0, max_size=8),
    reply=st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""),
)
def test_recovery_never_fires_on_clean_all_success(names: list[str], reply: str) -> None:
    """C4 negative: a transcript where every tool call succeeded, the
    turn stopped naturally, and the final reply is non-empty must
    classify as ``none`` regardless of the calls' shape."""
    # Distinct args per call so even identical names can't look like a
    # retry; all succeed.
    calls = [_call(name, ok=True, args={"i": i}) for i, name in enumerate(names)]
    t = detect_trouble(status="ok", tool_calls=calls, assistant_text=reply)
    assert t == Trouble("none")

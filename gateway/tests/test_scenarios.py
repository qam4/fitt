"""Tests for :mod:`gateway.scenarios` — the Phase 12 multi-step
scenario artifact (the shared ``daily_news_summary`` case).

Two concerns:

* The scenario is genuinely *multi-step* (a fetch + a deliver), so
  the flat loop has a real chance to stumble — pin that the prompt
  asks for both.
* :func:`classify_news_outcome` folds an ``AgentLoopResult`` into the
  right structural label, reading only facts (executed tool calls +
  statuses, loop status, reply length) — never reply wording. Each
  outcome label gets a case, plus the precedence between them.
"""

from __future__ import annotations

from gateway.agent_loop import AgentLoopResult
from gateway.memory import PersistedToolCall
from gateway.scenarios import (
    TRANSIENT_OUTCOMES,
    Scenario,
    classify_news_outcome,
    daily_news_summary,
)


def _call(tool_name: str, *, ok: bool = True) -> PersistedToolCall:
    return PersistedToolCall(
        tool_name=tool_name,
        args={},
        result_status="ok" if ok else "error",
        result_summary="" if ok else "boom",
    )


def _result(
    *,
    status: str = "ok",
    assistant_text: str = "",
    calls: list[PersistedToolCall] | None = None,
) -> AgentLoopResult:
    return AgentLoopResult(
        status=status,
        assistant_text=assistant_text,
        tool_calls_for_memory=calls or [],
    )


# --------------------------------------------------------------- the case


def test_daily_news_summary_is_multistep() -> None:
    sc = daily_news_summary()
    assert sc.name == "daily_news_summary"
    # The prompt must ask to both fetch and summarize — a single-step
    # phrasing would not exercise the chaining the planner targets.
    msg = sc.user_message.lower()
    assert "search" in msg
    assert "summary" in msg or "summarize" in msg
    assert sc.min_searches == 1


# --------------------------------------------------------------- outcomes


def test_completed_via_send_message() -> None:
    r = _result(calls=[_call("web_search"), _call("send_message")])
    assert classify_news_outcome(r) == "completed"


def test_completed_via_substantive_inline_reply() -> None:
    # Searched, then answered inline with a real summary (no
    # send_message). Length over the substantive threshold counts as
    # delivered.
    r = _result(
        assistant_text="- Headline one with detail. " * 12,
        calls=[_call("web_search")],
    )
    assert classify_news_outcome(r) == "completed"


def test_searched_not_delivered_thin_reply() -> None:
    # Fetched but stalled: a thin reply, no send_message, loop ended
    # naturally (not budget-exhausted).
    r = _result(assistant_text="ok", calls=[_call("web_search")])
    assert classify_news_outcome(r) == "searched_not_delivered"


def test_no_search_when_never_fetched() -> None:
    # Answered from training data / refused — never called web_search.
    r = _result(assistant_text="I can't access real-time information.")
    assert classify_news_outcome(r) == "no_search"


def test_tool_error_when_search_errored() -> None:
    r = _result(calls=[_call("web_search", ok=False)])
    assert classify_news_outcome(r) == "tool_error"


def test_exhausted_when_budget_hit_without_search() -> None:
    r = _result(status="tool_loop_exhausted", assistant_text="thinking...")
    assert classify_news_outcome(r) == "exhausted"


def test_exhausted_takes_precedence_over_not_delivered() -> None:
    # Fetched, but ran out of budget before delivering — budget signal
    # wins over the generic "searched_not_delivered".
    r = _result(
        status="tool_loop_exhausted",
        assistant_text="partial",
        calls=[_call("web_search")],
    )
    assert classify_news_outcome(r) == "exhausted"


def test_upstream_error_is_transient() -> None:
    r = _result(status="upstream_error")
    assert classify_news_outcome(r) == "upstream_error"
    assert "upstream_error" in TRANSIENT_OUTCOMES


def test_upstream_error_wins_even_with_calls() -> None:
    # A dispatch failure means nothing else is knowable, even if some
    # earlier tool calls landed.
    r = _result(status="upstream_error", calls=[_call("web_search")])
    assert classify_news_outcome(r) == "upstream_error"


def test_min_searches_threshold_respected() -> None:
    sc = Scenario(
        name="two_search",
        user_message="search twice then summarize",
        min_searches=2,
    )
    # Only one successful search → fetch step not satisfied.
    one = _result(assistant_text="x" * 300, calls=[_call("web_search")])
    assert classify_news_outcome(one, sc) == "no_search"
    # Two successful searches + substantive reply → completed.
    two = _result(
        assistant_text="x" * 300,
        calls=[_call("web_search"), _call("web_search")],
    )
    assert classify_news_outcome(two, sc) == "completed"

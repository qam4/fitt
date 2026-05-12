"""Tests for Phase 4.8b — :mod:`fitt_telegram_bot.turn_renderer`.

Covers the state machine's response to every turn-event kind
we render, plus:

* Lazy stream-bubble creation (only on first tool plan or
  when the caller explicitly forces via a reply-token append
  after a tool has planned).
* Stream-bubble coalescing (rapid events within the rate-limit
  window land as one edit).
* Approval bubble post + edit-in-place on decision.
* Finish footer posts for tool turns; skipped for pure chat.
* Tool-call executed rewrites the planned line (not a new row).
* State-guard: events after ``turn_finished`` are no-ops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from fitt_telegram_bot.turn_renderer import (
    MIN_STREAM_EDIT_INTERVAL_S,
    TurnPhase,
    TurnRenderer,
)

# --------------------------------------------------------------- recording stub


@dataclass
class _SentMessage:
    message_id: int


@dataclass
class _BotCall:
    op: str  # "send" | "edit"
    chat_id: int
    text: str
    message_id: int | None = None
    reply_markup: Any = None
    disable_notification: bool = False


@dataclass
class _FakeBot:
    """Records calls for later assertion. Assigns a monotonic
    message_id starting at 1000 so tests can distinguish which
    post a later edit targeted."""

    calls: list[_BotCall] = field(default_factory=list)
    _next_id: int = 1000

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any = None,
        disable_notification: bool = False,
    ) -> _SentMessage:
        self._next_id += 1
        self.calls.append(
            _BotCall(
                op="send",
                chat_id=chat_id,
                text=text,
                message_id=self._next_id,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
        )
        return _SentMessage(message_id=self._next_id)

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any = None,
    ) -> None:
        self.calls.append(
            _BotCall(
                op="edit",
                chat_id=chat_id,
                text=text,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        )

    def sends(self) -> list[_BotCall]:
        return [c for c in self.calls if c.op == "send"]

    def edits(self) -> list[_BotCall]:
        return [c for c in self.calls if c.op == "edit"]


class _Clock:
    """Monotonic-compatible clock with controllable advancement."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def advance(self, secs: float) -> None:
        self.t += secs

    def __call__(self) -> float:
        return self.t


def _approval_keyboard(_approval_id: str) -> str:
    """Stub keyboard builder — returns the approval_id for
    easy assertion rather than a real PTB markup."""
    return f"kb:{_approval_id}"


def _make_renderer(bot: _FakeBot, clock: _Clock) -> TurnRenderer:
    return TurnRenderer(
        bot,  # type: ignore[arg-type]
        chat_id=42,
        turn_id="t-1",
        build_approval_keyboard=_approval_keyboard,  # type: ignore[arg-type]
        clock=clock,
    )


# --------------------------------------------------------------- lifecycle


async def test_turn_started_is_ui_noop() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event({"kind": "turn_started", "meta": {"alias": "fitt-smart"}})
    assert bot.calls == []
    assert r.state.phase is TurnPhase.PENDING


async def test_pure_chat_turn_has_no_stream_bubble_no_footer() -> None:
    """A turn with no tool_call_planned and no
    approval_requested stays quiet — the chat reply message
    is the ping. turn_finished is a no-op in that case."""
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event({"kind": "turn_started", "meta": {}})
    await r.handle_event(
        {
            "kind": "turn_finished",
            "meta": {"status": "ok", "iterations": 1, "final_reply_len": 42},
        }
    )
    assert bot.calls == []
    assert r.state.phase is TurnPhase.FINISHED


# --------------------------------------------------------------- tool lifecycle


async def test_tool_call_planned_posts_stream_bubble() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {
                "tool_name": "read_file",
                "args": {"path": "README.md"},
                "call_id": "c1",
            },
        }
    )
    sends = bot.sends()
    assert len(sends) == 1
    assert "Reading" in sends[0].text
    assert "README.md" in sends[0].text
    assert sends[0].disable_notification is True  # silent
    assert r.state.stream_message_id == sends[0].message_id
    assert r.state.phase is TurnPhase.ACTIVE


async def test_tool_call_executed_rewrites_planned_line() -> None:
    """The executed event should UPDATE the planned line in the
    same bubble — not append a second row, not post a new
    bubble."""
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {
                "tool_name": "read_file",
                "args": {"path": "README.md"},
                "call_id": "c1",
            },
        }
    )
    planned_text = bot.sends()[-1].text
    assert planned_text.startswith("🔵")

    await r.handle_event(
        {
            "kind": "tool_call_executed",
            "meta": {
                "tool_name": "read_file",
                "call_id": "c1",
                "ok": True,
                "duration_ms": 12,
                "result_summary": "…",
            },
        }
    )
    edits = bot.edits()
    assert len(edits) == 1
    assert "✅ Read" in edits[0].text
    assert "12ms" in edits[0].text
    # Edit targeted the same message as the original post.
    assert edits[0].message_id == r.state.stream_message_id


async def test_tool_call_executed_failure_renders_error() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "x"}, "call_id": "c1"},
        }
    )
    await r.handle_event(
        {
            "kind": "tool_call_executed",
            "meta": {
                "tool_name": "read_file",
                "call_id": "c1",
                "ok": False,
                "duration_ms": 3,
                "result_summary": "file not found",
            },
        }
    )
    last = bot.edits()[-1]
    assert "❌" in last.text
    assert "file not found" in last.text


async def test_multiple_tools_accumulate_in_same_bubble() -> None:
    """Two tool_call_planned events post ONE bubble with two
    rows; a second execute updates its specific row."""
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "a"}, "call_id": "c1"},
        }
    )
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "write_file", "args": {"path": "b"}, "call_id": "c2"},
        }
    )
    # Only one send overall (the initial bubble); second plan
    # edits the existing bubble to add the new row.
    assert len(bot.sends()) == 1
    latest = (bot.edits() or bot.sends())[-1]
    assert "Reading" in latest.text
    assert "Writing" in latest.text
    assert latest.text.count("🔵") == 2

    await r.handle_event(
        {
            "kind": "tool_call_executed",
            "meta": {
                "tool_name": "write_file",
                "call_id": "c2",
                "ok": True,
                "duration_ms": 5,
            },
        }
    )
    # The edit replaces c2's line but leaves c1's placeholder.
    last_edit = bot.edits()[-1]
    assert "🔵 Reading" in last_edit.text
    assert "✅ Wrote" in last_edit.text


# --------------------------------------------------------------- approvals


async def test_approval_requested_posts_notifying_bubble() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "approval_requested",
            "meta": {
                "approval_id": "a1",
                "tool_name": "edit_file",
                "bucket": "ask",
                "client": "telegram",
            },
        }
    )
    sends = bot.sends()
    assert len(sends) == 1
    assert sends[0].disable_notification is False  # notifies
    assert "edit_file" in sends[0].text
    assert sends[0].reply_markup == "kb:a1"
    assert r.state.approval_bubbles["a1"] == sends[0].message_id


async def test_approval_decided_edits_in_place() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "approval_requested",
            "meta": {
                "approval_id": "a1",
                "tool_name": "edit_file",
                "bucket": "ask",
                "client": "telegram",
            },
        }
    )
    approval_bubble_id = r.state.approval_bubbles["a1"]

    await r.handle_event(
        {
            "kind": "approval_decided",
            "meta": {"approval_id": "a1", "decision": "approve", "duration_ms": 1500},
        }
    )
    edits = bot.edits()
    assert len(edits) == 1
    assert edits[0].message_id == approval_bubble_id
    assert "approved" in edits[0].text.lower()
    assert edits[0].reply_markup is None  # buttons cleared


async def test_approval_decided_unknown_id_is_silent_noop() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "approval_decided",
            "meta": {"approval_id": "ghost", "decision": "approve", "duration_ms": 1},
        }
    )
    assert bot.calls == []


async def test_approval_rejected_rendered_as_rejected() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "approval_requested",
            "meta": {"approval_id": "a1", "tool_name": "x", "bucket": "ask", "client": "tg"},
        }
    )
    await r.handle_event(
        {
            "kind": "approval_decided",
            "meta": {"approval_id": "a1", "decision": "reject", "duration_ms": 500},
        }
    )
    assert "rejected" in bot.edits()[-1].text.lower()


# --------------------------------------------------------------- finish footer


async def test_turn_finished_posts_footer_when_tools_ran() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "x"}, "call_id": "c1"},
        }
    )
    await r.handle_event(
        {
            "kind": "turn_finished",
            "meta": {"status": "ok", "iterations": 2, "final_reply_len": 42},
        }
    )
    # Stream bubble send + finish footer send = 2 sends total.
    assert len(bot.sends()) == 2
    footer = bot.sends()[-1]
    assert "Finished" in footer.text
    assert footer.disable_notification is False  # notifies


async def test_turn_finished_failure_renders_failure_footer() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "x"}, "call_id": "c1"},
        }
    )
    await r.handle_event(
        {
            "kind": "turn_finished",
            "meta": {"status": "tool_loop_exhausted", "iterations": 10},
        }
    )
    footer = bot.sends()[-1]
    assert "exhausted" in footer.text.lower()


# --------------------------------------------------------------- reply-token streaming


async def test_reply_tokens_land_in_stream_bubble_after_tool() -> None:
    """Once a tool has planned, subsequent reply-token deltas
    are appended to the stream bubble rather than a separate
    message."""
    bot = _FakeBot()
    clock = _Clock()
    r = _make_renderer(bot, clock)
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "x"}, "call_id": "c1"},
        }
    )
    # Force rate-limit window elapsed so append triggers edit.
    clock.advance(MIN_STREAM_EDIT_INTERVAL_S + 0.1)
    await r.append_reply_text("Here's what I found: ")
    clock.advance(MIN_STREAM_EDIT_INTERVAL_S + 0.1)
    await r.append_reply_text("foo bar.")
    last = bot.edits()[-1]
    assert "Here's what I found:" in last.text
    assert "foo bar." in last.text


async def test_reply_tokens_coalesce_within_rate_limit_window() -> None:
    """A burst of deltas should NOT produce one edit per delta.
    Within the 1s edit window, appends buffer until the next
    allowable edit."""
    bot = _FakeBot()
    clock = _Clock()
    r = _make_renderer(bot, clock)
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "x"}, "call_id": "c1"},
        }
    )
    # All appends in the SAME rate-limit window; no edits should
    # fire beyond the plan-forced edit.
    edits_before = len(bot.edits())
    for chunk in ("a", "b", "c", "d"):
        await r.append_reply_text(chunk)
    assert len(bot.edits()) == edits_before


async def test_pure_chat_reply_does_not_open_stream_bubble() -> None:
    """When no tool has planned, ``append_reply_text`` is a
    no-op for the bubble — the chat handler routes reply
    tokens to the legacy streaming path in that case."""
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.append_reply_text("hello")
    assert bot.calls == []
    assert r.state.stream_message_id is None
    assert r.should_create_stream_bubble() is False


# --------------------------------------------------------------- state guard


async def test_events_after_turn_finished_are_noop() -> None:
    bot = _FakeBot()
    r = _make_renderer(bot, _Clock())
    await r.handle_event(
        {
            "kind": "tool_call_planned",
            "meta": {"tool_name": "read_file", "args": {"path": "x"}, "call_id": "c1"},
        }
    )
    await r.handle_event({"kind": "turn_finished", "meta": {"status": "ok", "iterations": 1}})
    sends_before = len(bot.sends())
    edits_before = len(bot.edits())
    # Late event.
    await r.handle_event(
        {
            "kind": "tool_call_executed",
            "meta": {"tool_name": "read_file", "call_id": "c1", "ok": True, "duration_ms": 1},
        }
    )
    assert len(bot.sends()) == sends_before
    assert len(bot.edits()) == edits_before


# --------------------------------------------------------------- pytest-asyncio


@pytest.fixture(autouse=True)
def _asyncio_mode() -> None:
    """No-op; asyncio_mode=auto in pyproject.toml handles it.
    Present so a future tightening (say, selective opt-in)
    doesn't silently skip these tests."""

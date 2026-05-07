"""Tests for Phase 4.5 Task 6 — ``send_message`` inline tool.

Coverage (matches the spec's 6e enumeration):

* **6b rate limit:** triggers at the configured ceiling, returns
  a structured error with ``retry_after_secs`` as JSON in the
  payload, and lets the window slide.
* **6c event emission:** successful calls append an
  ``agent_message`` entry to the event log with the right
  ``session_key`` + ``meta`` shape.
* **6d no-push-channel warning:** fires exactly once per builder
  when ``push_channel_available`` returns False.
* **Input validation:** empty / missing / oversized ``text``
  and non-string ``title`` all return ``ToolResult.error``.

Also exercises the ``SendMessageRateLimiter`` directly so the
sliding-window logic is pinned independent of the tool glue.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from gateway.events import EventLog, default_events_path
from gateway.projects import ProjectRegistry
from gateway.tools import (
    SendMessageRateLimiter,
    ToolContext,
    ToolPolicy,
    build_send_message_tool,
)

# --------------------------------------------------------------- helpers


def _ctx(
    *,
    tmp_path: Path,
    events: EventLog | None = None,
    session: str = "main",
    client: str = "telegram",
    policy: ToolPolicy | None = None,
) -> ToolContext:
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    return ToolContext(
        client=client,
        session_key=session,
        projects=reg,
        events=events,
        policy=policy,
    )


@pytest.fixture
def events(tmp_path: Path) -> EventLog:
    # Use a subfolder rather than "fitt-home" since conftest's
    # autouse isolate_fitt_home fixture already creates that.
    home = tmp_path / "events-home"
    home.mkdir()
    return EventLog(default_events_path(home))


# --------------------------------------------------------------- rate limiter unit


def test_limiter_allows_up_to_ceiling() -> None:
    lim = SendMessageRateLimiter(window_secs=60.0, max_per_window=3)
    for _ in range(3):
        accepted, retry = lim.try_acquire("s1", now=100.0)
        assert accepted is True
        assert retry == 0.0
    accepted, retry = lim.try_acquire("s1", now=100.0)
    assert accepted is False
    # First slot is at t=100, window is 60s → retry at t=160, so
    # now-offset = 60.0s.
    assert retry == pytest.approx(60.0, abs=0.05)


def test_limiter_slides_window() -> None:
    lim = SendMessageRateLimiter(window_secs=60.0, max_per_window=2)
    # Two sends at t=0.
    lim.try_acquire("s1", now=0.0)
    lim.try_acquire("s1", now=0.0)
    blocked, _ = lim.try_acquire("s1", now=10.0)
    assert blocked is False
    # Advance past the window — the old entries age out.
    allowed, _ = lim.try_acquire("s1", now=61.0)
    assert allowed is True


def test_limiter_isolates_sessions() -> None:
    lim = SendMessageRateLimiter(window_secs=60.0, max_per_window=1)
    assert lim.try_acquire("s1", now=0.0)[0] is True
    # s1 is at its ceiling ...
    assert lim.try_acquire("s1", now=1.0)[0] is False
    # ... but s2 has its own bucket.
    assert lim.try_acquire("s2", now=1.0)[0] is True


def test_limiter_reset_clears_session() -> None:
    lim = SendMessageRateLimiter(window_secs=60.0, max_per_window=1)
    lim.try_acquire("s1", now=0.0)
    assert lim.try_acquire("s1", now=1.0)[0] is False
    lim.reset("s1")
    assert lim.try_acquire("s1", now=2.0)[0] is True


# --------------------------------------------------------------- happy path


async def test_send_message_emits_agent_message_event(tmp_path: Path, events: EventLog) -> None:
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events, session="main", client="ide")

    result = await tool.callable({"text": "build finished, all green.", "title": "CI"}, ctx)
    assert not result.is_error
    assert result.payload == "sent"

    entries = events.read()
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "agent_message"
    assert e.session_key == "main"
    assert e.title == "CI"
    assert e.body == "build finished, all green."
    assert e.meta["tool"] == "send_message"
    assert e.meta["client"] == "ide"


async def test_send_message_default_title_when_empty(tmp_path: Path, events: EventLog) -> None:
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events)

    await tool.callable({"text": "hi"}, ctx)
    entries = events.read()
    assert entries[0].title == "Agent Message"


# --------------------------------------------------------------- rate-limit path


async def test_send_message_rate_limit_structured_error(tmp_path: Path, events: EventLog) -> None:
    limiter = SendMessageRateLimiter(window_secs=60.0, max_per_window=2)
    tool = build_send_message_tool(limiter=limiter)
    ctx = _ctx(tmp_path=tmp_path, events=events)

    # Exhaust the ceiling.
    for i in range(2):
        r = await tool.callable({"text": f"msg {i}"}, ctx)
        assert not r.is_error

    # Next one trips the rate limit.
    r = await tool.callable({"text": "msg overflow"}, ctx)
    assert r.is_error
    # Payload is JSON per the spec so the model can parse the
    # retry_after_secs field and back off.
    payload = json.loads(r.payload)
    assert payload["error"] == "rate_limit_exceeded"
    assert "retry_after_secs" in payload
    assert payload["retry_after_secs"] > 0
    assert payload["window_secs"] == 60.0
    assert payload["max_per_window"] == 2

    # Only two agent_message events landed, not three.
    assert sum(1 for e in events.read() if e.kind == "agent_message") == 2


async def test_send_message_rate_limit_window_slides(tmp_path: Path, events: EventLog) -> None:
    """Use the limiter's injectable clock via its direct API to
    verify the tool honours a sliding window. We can't easily
    inject ``now`` through the tool call, so exercise the slide
    via the limiter's ``reset`` after the initial saturation —
    functionally equivalent (both land us back at zero sends)."""
    limiter = SendMessageRateLimiter(window_secs=60.0, max_per_window=1)
    tool = build_send_message_tool(limiter=limiter)
    ctx = _ctx(tmp_path=tmp_path, events=events)

    r1 = await tool.callable({"text": "one"}, ctx)
    assert not r1.is_error
    r2 = await tool.callable({"text": "two"}, ctx)
    assert r2.is_error and "rate_limit_exceeded" in r2.payload

    # Simulate the window sliding by resetting this session.
    limiter.reset("main")
    r3 = await tool.callable({"text": "three"}, ctx)
    assert not r3.is_error


async def test_rate_limit_isolates_sessions(tmp_path: Path, events: EventLog) -> None:
    limiter = SendMessageRateLimiter(window_secs=60.0, max_per_window=1)
    tool = build_send_message_tool(limiter=limiter)
    ctx_a = _ctx(tmp_path=tmp_path, events=events, session="sessA")
    ctx_b = _ctx(tmp_path=tmp_path, events=events, session="sessB")

    r = await tool.callable({"text": "a"}, ctx_a)
    assert not r.is_error
    # sessA saturated; sessB still free.
    r = await tool.callable({"text": "a again"}, ctx_a)
    assert r.is_error
    r = await tool.callable({"text": "b"}, ctx_b)
    assert not r.is_error


# --------------------------------------------------------------- no-push-channel warning


async def test_no_push_channel_warns_once(
    tmp_path: Path,
    events: EventLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool = build_send_message_tool(push_channel_available=lambda: False)
    ctx = _ctx(tmp_path=tmp_path, events=events)

    with caplog.at_level(logging.WARNING):
        for i in range(3):
            r = await tool.callable({"text": f"msg {i}"}, ctx)
            assert not r.is_error

    warnings = [r for r in caplog.records if "no_push_channel" in r.getMessage()]
    # Exactly one warning, even though we sent 3 messages.
    assert len(warnings) == 1
    # All three events still landed — the warning is operator UX,
    # not a guard.
    assert sum(1 for e in events.read() if e.kind == "agent_message") == 3


async def test_push_channel_available_skips_warning(
    tmp_path: Path,
    events: EventLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tool = build_send_message_tool(push_channel_available=lambda: True)
    ctx = _ctx(tmp_path=tmp_path, events=events)

    with caplog.at_level(logging.WARNING):
        r = await tool.callable({"text": "hello"}, ctx)
        assert not r.is_error

    assert not any("no_push_channel" in r.getMessage() for r in caplog.records)


async def test_probe_callable_raising_does_not_break_tool(
    tmp_path: Path,
    events: EventLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the push-channel probe itself raises, we log debug and
    proceed as if the channel is available (i.e. don't warn).
    Warning about "no push channel" when we can't even probe
    would be misleading operator UX."""

    def _boom() -> bool:
        raise RuntimeError("config gone sideways")

    tool = build_send_message_tool(push_channel_available=_boom)
    ctx = _ctx(tmp_path=tmp_path, events=events)

    r = await tool.callable({"text": "hi"}, ctx)
    assert not r.is_error
    # No WARNING emitted either way.
    assert not any("no_push_channel" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------- validation


async def test_missing_text_returns_error(tmp_path: Path, events: EventLog) -> None:
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events)

    r = await tool.callable({}, ctx)
    assert r.is_error
    assert "required" in r.payload.lower()


async def test_empty_text_returns_error(tmp_path: Path, events: EventLog) -> None:
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events)

    r = await tool.callable({"text": "   "}, ctx)
    assert r.is_error


async def test_oversized_text_rejected(tmp_path: Path, events: EventLog) -> None:
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events)

    r = await tool.callable({"text": "x" * 4001}, ctx)
    assert r.is_error
    assert "exceeds" in r.payload.lower()


async def test_non_string_title_rejected(tmp_path: Path, events: EventLog) -> None:
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events)

    r = await tool.callable({"text": "hi", "title": 42}, ctx)  # type: ignore[dict-item]
    assert r.is_error
    assert "string" in r.payload.lower()


async def test_missing_event_log_surfaces_error(tmp_path: Path) -> None:
    """When ``ctx.events is None`` the tool fails readably rather
    than silently losing the send. Rate-limit slot is still
    consumed — otherwise the model could spam the tool as a
    free no-op."""
    limiter = SendMessageRateLimiter(window_secs=60.0, max_per_window=2)
    tool = build_send_message_tool(limiter=limiter)
    ctx = _ctx(tmp_path=tmp_path, events=None)

    r = await tool.callable({"text": "hi"}, ctx)
    assert r.is_error
    assert "event log not wired" in r.payload

    # Rate-limit bucket was consumed: one send in the last minute.
    assert limiter.current("main") == 1


# --------------------------------------------------------------- policy-driven config


async def test_policy_overrides_limiter_defaults(tmp_path: Path, events: EventLog) -> None:
    """config.yaml's ``tools.send_message.window_secs`` /
    ``max_per_window`` propagate to the limiter on the first
    call."""
    policy = ToolPolicy.from_config(
        {
            "send_message": {
                "window_secs": 120,
                "max_per_window": 1,
            }
        }
    )
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events, policy=policy)

    r = await tool.callable({"text": "one"}, ctx)
    assert not r.is_error
    # Per-window cap is 1 — next one trips.
    r = await tool.callable({"text": "two"}, ctx)
    assert r.is_error
    payload = json.loads(r.payload)
    assert payload["window_secs"] == 120.0
    assert payload["max_per_window"] == 1


async def test_policy_bad_values_logged_but_not_fatal(
    tmp_path: Path,
    events: EventLog,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed policy values log a warning but the tool still
    runs against its last-known-good (or default) settings."""
    policy = ToolPolicy.from_config(
        {
            "send_message": {
                "window_secs": "not-a-number",
                "max_per_window": "also-bogus",
            }
        }
    )
    tool = build_send_message_tool()
    ctx = _ctx(tmp_path=tmp_path, events=events, policy=policy)

    with caplog.at_level(logging.WARNING):
        r = await tool.callable({"text": "hi"}, ctx)
    assert not r.is_error
    assert any("bad_policy_value" in r.getMessage() for r in caplog.records)

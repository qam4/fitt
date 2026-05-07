"""Tests for Phase 4.5 Task 7 — the EventPusher.

The pusher polls the gateway's ``/v1/events`` endpoint, keeps a
cursor on disk so a bot restart doesn't replay history or miss
events, skips approval events (they have their own delivery
surface), and never lets a per-user send failure wedge the cursor.

Tests stub the ``GatewayClient.list_events`` method so we don't
need a live gateway; cursor file is written to ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from fitt_telegram_bot.event_pusher import EventPusher, _CursorStore


class _FakeGateway:
    """Queueable stub of ``GatewayClient.list_events``.

    Each call pops the front of ``queued_responses``; when empty,
    returns []. Records all calls so tests can assert on
    ``since`` evolution.
    """

    def __init__(self) -> None:
        self.queued_responses: list[list[dict[str, Any]]] = []
        self.calls: list[dict[str, Any]] = []

    async def list_events(
        self, *, since: float | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.calls.append({"since": since, "limit": limit})
        if self.queued_responses:
            return self.queued_responses.pop(0)
        return []


def _evt(kind: str, ts: float, body: str = "body", title: str = "title") -> dict[str, Any]:
    return {
        "ts": ts,
        "kind": kind,
        "session_key": "main",
        "title": title,
        "body": body,
        "meta": {},
    }


async def _tick(pusher: EventPusher) -> None:
    """Drive one iteration of the pusher's internal tick."""
    await pusher._tick()


# --------------------------------------------------------------- cursor store


def test_cursor_missing_file_returns_none(tmp_path: Path) -> None:
    store = _CursorStore(tmp_path / "cursor.json")
    assert store.load() is None


def test_cursor_round_trip(tmp_path: Path) -> None:
    store = _CursorStore(tmp_path / "cursor.json")
    store.save(1234.5)
    assert store.load() == 1234.5


def test_cursor_corrupt_file_returns_none(tmp_path: Path) -> None:
    """A half-written or hand-edited-to-garbage cursor should
    degrade gracefully to "start fresh" rather than crash the
    pusher."""
    p = tmp_path / "cursor.json"
    p.write_text("not json", encoding="utf-8")
    store = _CursorStore(p)
    assert store.load() is None


def test_cursor_unexpected_shape_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "cursor.json"
    p.write_text(json.dumps({"some_other_field": 123}), encoding="utf-8")
    store = _CursorStore(p)
    assert store.load() is None


# --------------------------------------------------------------- first boot


async def test_first_boot_no_cursor_starts_from_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No cursor file on disk: the pusher must NOT replay the
    gateway's historical events. We anchor at "now" so the user
    only sees fresh activity."""
    monkeypatch.setattr("time.time", lambda: 5000.0)
    gw = _FakeGateway()
    gw.queued_responses.append(
        [
            _evt("cron_completed", ts=100.0),  # ancient
            _evt("cron_completed", ts=200.0),  # still ancient
        ]
    )
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,  # type: ignore[arg-type]
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    # Nothing pushed: all events are older than our "now" = 5000.
    assert sent == []
    # Cursor stayed at 5000 because we never advanced past it.
    assert pusher._last_ts == 5000.0


async def test_first_boot_honours_cursor_on_restart(tmp_path: Path) -> None:
    """A cursor file from a previous run is loaded and the
    pusher resumes after it. Events before the cursor stay
    skipped (already delivered or intentionally old); events
    after are pushed."""
    cursor_path = tmp_path / "cursor.json"
    _CursorStore(cursor_path).save(1000.0)

    gw = _FakeGateway()
    gw.queued_responses.append(
        [
            _evt("cron_completed", ts=500.0),  # before cursor; strict-greater filter at gateway
            _evt("cron_completed", ts=1500.0),  # after cursor; pushed
        ]
    )
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=cursor_path,
    )
    # The pusher asks the gateway with since=1000.0 — the gateway
    # filters strict-greater — our stub returns the whole queue
    # unfiltered, so the pusher's own loop must also skip the 500.
    await _tick(pusher)
    assert len(sent) == 1
    assert pusher._last_ts == 1500.0
    # Cursor is persisted for the next restart.
    assert _CursorStore(cursor_path).load() == 1500.0


# --------------------------------------------------------------- delivery


async def test_events_delivered_in_order(tmp_path: Path) -> None:
    gw = _FakeGateway()
    gw.queued_responses.append(
        [
            _evt("cron_completed", ts=100.0, body="first"),
            _evt("cron_completed", ts=200.0, body="second"),
        ]
    )
    _CursorStore(tmp_path / "cursor.json").save(50.0)
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    # format_event prefixes kind-specific headers; we only assert
    # the body made it through in the right order.
    assert "first" in sent[0]
    assert "second" in sent[1]


async def test_cursor_advances_monotonically_across_ticks(tmp_path: Path) -> None:
    gw = _FakeGateway()
    gw.queued_responses.append([_evt("cron_completed", ts=100.0)])
    gw.queued_responses.append([_evt("cron_completed", ts=200.0)])
    _CursorStore(tmp_path / "cursor.json").save(50.0)
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    await _tick(pusher)
    # Each tick asked for events strictly after the previous cursor.
    assert gw.calls[0]["since"] == 50.0
    assert gw.calls[1]["since"] == 100.0
    assert pusher._last_ts == 200.0


# --------------------------------------------------------------- skip approvals


async def test_approval_events_are_skipped(tmp_path: Path) -> None:
    """Approvals have a dedicated delivery pipeline (inline
    keyboard via ApprovalPoller). Double-delivering them here
    would spam the user with plain-text copies that can't be
    acted on."""
    gw = _FakeGateway()
    gw.queued_responses.append(
        [
            _evt("approval_requested", ts=100.0),
            _evt("approval_resolved", ts=150.0),
            _evt("cron_completed", ts=200.0, body="real push"),
        ]
    )
    _CursorStore(tmp_path / "cursor.json").save(50.0)
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    # Only the cron_completed made it to delivery.
    assert len(sent) == 1
    assert "real push" in sent[0]
    # Cursor advanced past ALL of them, including the skipped
    # approval events, so we don't re-scan them next tick.
    assert pusher._last_ts == 200.0


async def test_cron_fired_is_skipped(tmp_path: Path) -> None:
    """cron_fired is internal bookkeeping, not a user-facing
    moment. Pushing it would give the user a '✅ cron X' ping
    followed by the actual cron_completed message two seconds
    later — reads as a duplicate and adds no information.

    Observed 2026-05-07: without this skip, the user's phone
    lit up twice per cron firing (once for cron_fired, once
    for cron_completed). Keeping cron_completed is what the
    user actually wants; cron_fired is for events.jsonl and
    the gateway log, not for Telegram."""
    gw = _FakeGateway()
    gw.queued_responses.append(
        [
            _evt("cron_fired", ts=100.0, title="cron 'lunch'", body=""),
            _evt("cron_completed", ts=150.0, title="cron 'lunch'", body="go eat"),
        ]
    )
    _CursorStore(tmp_path / "cursor.json").save(50.0)
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    # Only the cron_completed was delivered.
    assert len(sent) == 1
    assert "go eat" in sent[0]
    # Cursor advanced past both.
    assert pusher._last_ts == 150.0


# --------------------------------------------------------------- empty-body


async def test_empty_body_event_advances_cursor_without_pushing(tmp_path: Path) -> None:
    """A silent cron_completed has an empty body; the formatter
    produces an empty string; we must not send an empty Telegram
    message. Cursor still advances so the event isn't retried."""
    gw = _FakeGateway()
    # Empty body AND empty title → empty formatted output
    # (format_event fallback returns kind when everything else
    # is empty; but cron_completed uses title when body is empty,
    # so we synth a silent-cron shape).
    gw.queued_responses.append(
        [
            {
                "ts": 100.0,
                "kind": "cron_completed",
                "session_key": "cron:abc:100",
                "title": "",
                "body": "",
                "meta": {},
            },
            _evt("cron_completed", ts=200.0, body="real push"),
        ]
    )
    _CursorStore(tmp_path / "cursor.json").save(50.0)
    sent: list[str] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        sent.append(text)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    # Only the non-empty event made it through.
    assert len(sent) == 1
    assert "real push" in sent[0]
    # Cursor advanced past both.
    assert pusher._last_ts == 200.0


# --------------------------------------------------------------- send failure


async def test_per_user_send_failure_does_not_wedge_cursor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Per spec 7d: delivery failures are logged and do not block
    the producer. A 500 from Telegram's API or a single bad
    user_id in the allowlist must not leave the cursor behind
    its actual progress — otherwise the pusher would retry
    forever."""
    gw = _FakeGateway()
    gw.queued_responses.append([_evt("cron_completed", ts=100.0)])
    _CursorStore(tmp_path / "cursor.json").save(50.0)

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        raise RuntimeError("Telegram is down")

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    with caplog.at_level(logging.WARNING):
        await _tick(pusher)

    # Cursor still advanced despite the send failure.
    assert pusher._last_ts == 100.0
    assert _CursorStore(tmp_path / "cursor.json").load() == 100.0
    # And we logged the failure.
    assert any("send_failed" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------- multi-user


async def test_pushes_to_each_allowlisted_user(tmp_path: Path) -> None:
    gw = _FakeGateway()
    gw.queued_responses.append([_evt("cron_completed", ts=100.0)])
    _CursorStore(tmp_path / "cursor.json").save(50.0)
    recipients: list[int] = []

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        recipients.append(user_id)

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({1, 2, 3}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
    )
    await _tick(pusher)
    assert sorted(recipients) == [1, 2, 3]


# --------------------------------------------------------------- lifecycle


async def test_stop_exits_run_loop(tmp_path: Path) -> None:
    gw = _FakeGateway()
    _CursorStore(tmp_path / "cursor.json").save(0.0)

    async def _send(user_id: int, _entry: dict[str, Any], text: str) -> None:
        pass

    pusher = EventPusher(
        gateway=gw,
        allowlist=frozenset({42}),
        on_event=_send,
        cursor_path=tmp_path / "cursor.json",
        poll_interval_s=0.05,
    )
    task = asyncio.create_task(pusher.run())
    # Let it tick at least once.
    await asyncio.sleep(0.08)
    pusher.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()

"""Tests for :mod:`fitt_telegram_bot.turn_stream`.

Covers:

* ``ensure`` is idempotent per-session.
* Events land at the right renderer keyed by ``turn_id``.
* Multiple turn_ids → multiple renderer instances.
* ``turn_finished`` drops the renderer from the mux's state.
* Transport failure triggers reconnect with backoff.
* ``stop`` cancels running tasks.

Uses ``httpx.MockTransport`` so we don't need a real HTTP
server.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from fitt_telegram_bot.turn_stream import TurnStreamMultiplexer

# --------------------------------------------------------------- fake renderer


@dataclass
class _FakeRenderer:
    """Records events for later assertion; stands in for the
    real TurnRenderer."""

    session_id: str
    turn_id: str
    seen: list[dict[str, Any]] = field(default_factory=list)

    async def handle_event(self, event: dict[str, Any]) -> None:
        self.seen.append(event)


# --------------------------------------------------------------- SSE byte stream builder


def _sse_frames(frames: list[dict[str, Any]]) -> bytes:
    """Render events as on-wire SSE bytes matching the
    gateway's emission shape (``event: <kind>\\ndata: <json>\\n\\n``)."""
    chunks: list[str] = []
    for f in frames:
        chunks.append(f"event: {f.get('kind', '')}\ndata: {json.dumps(f)}\n\n")
    return "".join(chunks).encode("utf-8")


class _ByteStream:
    """httpx-compatible streaming response body. Yields the
    pre-built SSE bytes in one chunk, then EOF."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._delivered = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if not self._delivered:
            self._delivered = True
            yield self._payload

    async def aclose(self) -> None:
        pass


@pytest.fixture
def make_mock_transport() -> Iterator[Any]:
    """Factory fixture: tests call it with a list of event
    frames and get back an httpx MockTransport that returns
    them on the first /stream request."""
    yield _make_mock_transport


def _make_mock_transport(
    frames: list[dict[str, Any]],
    *,
    status: int = 200,
) -> httpx.MockTransport:
    body = _sse_frames(frames)

    def _responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    return httpx.MockTransport(_responder)


# --------------------------------------------------------------- helpers


def _mux_with_frames(
    frames: list[dict[str, Any]],
    *,
    status: int = 200,
) -> tuple[TurnStreamMultiplexer, dict[tuple[str, str], _FakeRenderer]]:
    """Build a multiplexer whose single connection returns
    ``frames``. Renderers are collected in the returned dict
    keyed by (session_id, turn_id)."""
    renderers: dict[tuple[str, str], _FakeRenderer] = {}

    def factory(session_id: str, turn_id: str) -> Any:
        renderer = _FakeRenderer(session_id=session_id, turn_id=turn_id)
        renderers[(session_id, turn_id)] = renderer
        return renderer

    mux = TurnStreamMultiplexer(
        base_url="http://test",
        bearer_token="token",
        renderer_factory=factory,  # type: ignore[arg-type]
    )
    # Patch the _consume_once to use a mock transport. The real
    # code constructs its own AsyncClient inside _consume_once;
    # monkey-patching the method is easier than threading a
    # transport in.
    transport = _make_mock_transport(frames, status=status)

    async def _consume_once(session_id: str) -> None:
        url = f"http://test/v1/sessions/{session_id}/turns/stream"
        async with httpx.AsyncClient(transport=transport) as http:
            async with http.stream("GET", url) as response:
                if response.status_code // 100 != 2:
                    raise httpx.HTTPError(f"status={response.status_code}")
                async for line in response.aiter_lines():
                    await mux._handle_line(session_id, line)  # type: ignore[attr-defined]

    mux._consume_once = _consume_once  # type: ignore[attr-defined]
    return mux, renderers


# --------------------------------------------------------------- tests


async def test_dispatches_event_to_renderer_keyed_by_turn_id() -> None:
    frames = [
        {"turn_id": "t1", "kind": "turn_started", "meta": {"alias": "fitt-smart"}},
        {"turn_id": "t1", "kind": "turn_finished", "meta": {"status": "ok", "iterations": 1}},
    ]
    mux, renderers = _mux_with_frames(frames)
    await mux.ensure("main", 42)
    # Wait for the subscriber task to consume the frames and
    # then exit cleanly (since MockTransport returns a bounded
    # response body → aiter_lines ends → _consume_once returns
    # → _run's outer loop sleeps `backoff` seconds, giving us
    # time to stop before it reconnects).
    await asyncio.sleep(0.1)
    await mux.stop()

    r = renderers[("main", "t1")]
    kinds = [e["kind"] for e in r.seen]
    assert kinds == ["turn_started", "turn_finished"]


async def test_multiple_turn_ids_spawn_multiple_renderers() -> None:
    frames = [
        {"turn_id": "t1", "kind": "turn_started"},
        {"turn_id": "t2", "kind": "turn_started"},
        {"turn_id": "t1", "kind": "turn_finished", "meta": {"status": "ok", "iterations": 1}},
        {"turn_id": "t2", "kind": "turn_finished", "meta": {"status": "ok", "iterations": 1}},
    ]
    mux, renderers = _mux_with_frames(frames)
    await mux.ensure("main", 42)
    await asyncio.sleep(0.1)
    await mux.stop()

    assert ("main", "t1") in renderers
    assert ("main", "t2") in renderers
    # Each renderer saw its own events, not the other's.
    assert [e["kind"] for e in renderers[("main", "t1")].seen] == [
        "turn_started",
        "turn_finished",
    ]
    assert [e["kind"] for e in renderers[("main", "t2")].seen] == [
        "turn_started",
        "turn_finished",
    ]


async def test_turn_finished_drops_renderer_from_subscriber_state() -> None:
    """After turn_finished the renderer entry is removed from
    the subscriber's map, so an out-of-order late event for
    the same turn_id spawns a fresh renderer rather than
    appending to the finished one."""
    frames = [
        {"turn_id": "t1", "kind": "turn_started"},
        {"turn_id": "t1", "kind": "turn_finished", "meta": {"status": "ok", "iterations": 1}},
    ]
    mux, _ = _mux_with_frames(frames)
    await mux.ensure("main", 42)
    await asyncio.sleep(0.1)
    assert mux.get_renderer("main", "t1") is None
    await mux.stop()


async def test_ensure_is_idempotent_per_session() -> None:
    """Calling ``ensure`` twice for the same session does NOT
    open a second task."""
    mux, _ = _mux_with_frames([])
    await mux.ensure("main", 42)
    first_task = mux._subscribers["main"].task  # type: ignore[attr-defined]
    await mux.ensure("main", 42)
    second_task = mux._subscribers["main"].task  # type: ignore[attr-defined]
    assert first_task is second_task
    await mux.stop()


async def test_bad_status_raises_and_triggers_backoff_reconnect() -> None:
    """A 401/500 response surfaces as httpx.HTTPError which
    the outer loop catches; backoff-reconnect is the policy."""
    mux, _ = _mux_with_frames([], status=500)
    await mux.ensure("main", 42)
    # Give the subscriber one tick to hit the 500, bail out,
    # log, and enter the backoff sleep. We stop before the
    # retry fires.
    await asyncio.sleep(0.1)
    await mux.stop()
    # No crash, no leaked tasks; stop() returned cleanly.


async def test_stop_cancels_running_subscriber() -> None:
    mux, _ = _mux_with_frames([])
    await mux.ensure("main", 42)
    assert "main" in mux._subscribers  # type: ignore[attr-defined]
    await mux.stop()
    # After stop the subscribers dict is cleared.
    assert mux._subscribers == {}  # type: ignore[attr-defined]


async def test_malformed_frame_is_logged_and_skipped() -> None:
    """A data line that isn't valid JSON gets warned about
    and dropped; subsequent well-formed frames still land."""
    # Hand-construct a mixed stream: one junk data frame
    # plus one valid frame.
    body = (
        b"data: not-json\n\n"
        b"event: turn_started\n"
        b'data: {"turn_id": "t1", "kind": "turn_started"}\n\n'
    )

    def _responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(_responder)
    renderers: dict[tuple[str, str], _FakeRenderer] = {}

    def factory(session_id: str, turn_id: str) -> Any:
        r = _FakeRenderer(session_id=session_id, turn_id=turn_id)
        renderers[(session_id, turn_id)] = r
        return r

    mux = TurnStreamMultiplexer(
        base_url="http://test",
        bearer_token="token",
        renderer_factory=factory,  # type: ignore[arg-type]
    )

    async def _consume_once(session_id: str) -> None:
        url = f"http://test/v1/sessions/{session_id}/turns/stream"
        async with httpx.AsyncClient(transport=transport) as http:
            async with http.stream("GET", url) as response:
                async for line in response.aiter_lines():
                    await mux._handle_line(session_id, line)  # type: ignore[attr-defined]

    mux._consume_once = _consume_once  # type: ignore[attr-defined]
    await mux.ensure("main", 42)
    await asyncio.sleep(0.1)
    await mux.stop()
    # Valid frame still landed.
    assert ("main", "t1") in renderers
    assert renderers[("main", "t1")].seen[0]["kind"] == "turn_started"

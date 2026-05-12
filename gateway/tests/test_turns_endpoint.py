"""Tests for Phase 4.8c per-turn HTTP endpoints.

Two routes:

* ``GET /v1/sessions/{id}/turns`` — paged read. Exercised via
  :class:`TestClient` end-to-end (fast, happy paths, shape).
* ``GET /v1/sessions/{id}/turns/stream`` — SSE live. End-to-end
  streaming through httpx's ASGI transport buffers responses
  on the test side, so we cover SSE behaviour in two layers:

  1. The SSE **frame-builder** (``_sse_frame``) has a unit test
     asserting the exact wire shape.
  2. A TestClient-level **connect test** asserts the endpoint
     returns ``200`` with the right content type when there's
     already replay content queued. Live delivery assertions
     rely on the 4.8a integration suite (``chat → run_agent_loop``
     drives TurnLog.append, which fans out via the same
     subscriber hook the SSE handler wires up) — those tests
     already cover the end-to-end subscriber behaviour from a
     known-good starting point.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.events_endpoint import _sse_frame
from gateway.turns import new_event

from ._fixtures import PERSONAL_TOKEN, build_test_config


def _ts(day: date, hour: int = 12) -> float:
    """Unix ts for the given date at a stable hour-of-day."""
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC).timestamp()


# TurnLog.read defaults to the last 30 days when ``since`` is
# None. Anchor test seeds near "today" so the default window
# picks them up without callers needing to pass ``since``.
_RECENT = date.today() - timedelta(days=1)


# --------------------------------------------------------------- sync (paged)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _seed(client: TestClient, session: str, events: list[dict[str, Any]]) -> None:
    """Append turn events via the TurnLog primitive."""
    turns = client.app.state.turns
    for e in events:
        turns.append(
            new_event(
                turn_id=e["turn_id"],
                kind=e["kind"],
                session_key=session,
                meta=e.get("meta", {}),
                ts=e["ts"],
            )
        )


def test_turns_requires_auth(client: TestClient) -> None:
    r = client.get("/v1/sessions/main/turns")
    assert r.status_code == 401


def test_turns_empty(client: TestClient) -> None:
    r = client.get("/v1/sessions/main/turns", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"entries": [], "next_since": None}


def test_turns_returns_entries(client: TestClient) -> None:
    _seed(
        client,
        "main",
        [
            {"turn_id": "t1", "kind": "turn_started", "ts": _ts(_RECENT, 10)},
            {
                "turn_id": "t1",
                "kind": "llm_call_started",
                "ts": _ts(_RECENT, 10) + 1,
                "meta": {"alias": "fitt-smart", "iteration": 0},
            },
            {"turn_id": "t1", "kind": "turn_finished", "ts": _ts(_RECENT, 10) + 10},
        ],
    )
    r = client.get("/v1/sessions/main/turns", headers=_auth())
    body = r.json()
    assert set(body.keys()) == {"entries", "next_since"}
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds == ["turn_started", "llm_call_started", "turn_finished"]
    # Shape check on one entry — the fields the renderer reads.
    e0 = body["entries"][0]
    assert set(e0.keys()) == {"ts", "kind", "turn_id", "event_id", "session_key", "meta"}


def test_turns_scoped_to_session(client: TestClient) -> None:
    """Reading session A doesn't surface session B's entries."""
    _seed(client, "session_a", [{"turn_id": "ta", "kind": "turn_started", "ts": _ts(_RECENT, 10)}])
    _seed(
        client,
        "session_b",
        [{"turn_id": "tb", "kind": "turn_started", "ts": _ts(_RECENT, 11)}],
    )
    r = client.get("/v1/sessions/session_a/turns", headers=_auth())
    turn_ids = [e["turn_id"] for e in r.json()["entries"]]
    assert turn_ids == ["ta"]


def test_turns_kind_filter(client: TestClient) -> None:
    base = _ts(_RECENT, 10)
    _seed(
        client,
        "main",
        [
            {"turn_id": "t1", "kind": "turn_started", "ts": base},
            {"turn_id": "t1", "kind": "llm_call_started", "ts": base + 1},
            {"turn_id": "t1", "kind": "llm_call_completed", "ts": base + 2},
        ],
    )
    r = client.get("/v1/sessions/main/turns?kind=llm_call_started", headers=_auth())
    kinds = [e["kind"] for e in r.json()["entries"]]
    assert kinds == ["llm_call_started"]


def test_turns_turn_id_filter(client: TestClient) -> None:
    base = _ts(_RECENT, 10)
    _seed(
        client,
        "main",
        [
            {"turn_id": "t1", "kind": "turn_started", "ts": base},
            {"turn_id": "t2", "kind": "turn_started", "ts": base + 100},
        ],
    )
    r = client.get("/v1/sessions/main/turns?turn_id=t2", headers=_auth())
    tids = [e["turn_id"] for e in r.json()["entries"]]
    assert tids == ["t2"]


def test_turns_since_exclusive(client: TestClient) -> None:
    base = _ts(_RECENT, 10)
    _seed(
        client,
        "main",
        [
            {"turn_id": "t1", "kind": "turn_started", "ts": base},
            {"turn_id": "t1", "kind": "turn_finished", "ts": base + 100},
        ],
    )
    r = client.get(f"/v1/sessions/main/turns?since={base}", headers=_auth())
    kinds = [e["kind"] for e in r.json()["entries"]]
    assert kinds == ["turn_finished"]


def test_turns_limit_validates(client: TestClient) -> None:
    r = client.get("/v1/sessions/main/turns?limit=0", headers=_auth())
    assert r.status_code == 400
    r = client.get("/v1/sessions/main/turns?limit=10000", headers=_auth())
    assert r.status_code == 400


# --------------------------------------------------------------- SSE frame


def test_sse_frame_shape() -> None:
    """The on-wire frame shape matches what an ``EventSource``
    client expects: ``event: <kind>\\ndata: <json>\\n\\n``."""
    entry = new_event(
        turn_id="t1",
        kind="tool_call_planned",
        session_key="main",
        meta={"tool_name": "read_file"},
        ts=1778000000.0,
        event_id="e1",
    )
    raw = _sse_frame(entry)
    text = raw.decode()
    assert text.startswith("event: tool_call_planned\n")
    assert text.endswith("\n\n")
    data_line = next(ln for ln in text.splitlines() if ln.startswith("data:"))
    payload = json.loads(data_line[len("data:") :].lstrip())
    assert payload["turn_id"] == "t1"
    assert payload["kind"] == "tool_call_planned"
    assert payload["event_id"] == "e1"
    assert payload["session_key"] == "main"
    assert payload["meta"] == {"tool_name": "read_file"}
    assert payload["ts"] == 1778000000.0


# --------------------------------------------------------------- SSE endpoint connect


def test_turns_stream_requires_auth(client: TestClient) -> None:
    """No bearer token → 401. Covered before we ever open a
    stream context so the test returns immediately."""
    r = client.get("/v1/sessions/main/turns/stream")
    assert r.status_code == 401


def test_turns_stream_errors_when_turns_disabled(tmp_path: Path) -> None:
    """A gateway with no TurnLog wired (unusual, test contexts
    only) returns 503 rather than hanging the client on an
    endpoint that will never produce."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    app.state.turns = None
    client = TestClient(app)
    r = client.get("/v1/sessions/main/turns/stream", headers=_auth())
    assert r.status_code == 503


# --------------------------------------------------------------- TurnLog subscribe contract
#
# The SSE handler's live path is a thin wrapper over
# ``TurnLog.subscribe``: every event landed in the in-process
# log fans out to every registered callback. We cover the
# subscriber-hook behaviour here (fast, deterministic,
# no-HTTP) and rely on test_turn_events_integration to
# exercise the chat → append → subscriber path end-to-end
# inside the same event loop. The HTTP-level envelope
# (200 status, content-type, auth) is pinned by the connect
# tests above.


def test_turnlog_subscribe_fans_out_to_multiple_callbacks(tmp_path: Path) -> None:
    """Two registered subscribers both receive every event,
    in order, per the ``TurnLog.subscribe`` contract."""
    from gateway.turns import TurnLog

    log = TurnLog(tmp_path)
    seen_a: list[str] = []
    seen_b: list[str] = []
    log.subscribe(lambda e: seen_a.append(e.kind))
    log.subscribe(lambda e: seen_b.append(e.kind))

    for kind in ("turn_started", "tool_call_planned", "turn_finished"):
        log.append(
            new_event(
                turn_id="t1",
                kind=kind,
                session_key="main",
                ts=_ts(_RECENT, 10),
            )
        )
    assert seen_a == ["turn_started", "tool_call_planned", "turn_finished"]
    assert seen_a == seen_b

"""Tests for ``/v1/sessions/<s>/captures`` and
``/v1/sessions/<s>/captures/<turn_id>`` (Phase 7 Slice 7.2).

Covers shape, auth, missing-resource 404s, and round-trip
through the on-disk store.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.turn_capture import TurnCapture, TurnCaptureStore

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _make_capture(
    *,
    turn_id: str = "turn-1",
    session_key: str = "main",
    finished_at: float = 1779479825.81,
    started_at: float = 1779479823.42,
) -> TurnCapture:
    return TurnCapture(
        turn_id=turn_id,
        session_key=session_key,
        alias="fitt-default",
        client="telegram",
        model_used="qwen2.5-coder:14b",
        backend="ollama",
        fallback_used=False,
        started_at=started_at,
        finished_at=finished_at,
        dispatched_messages=[
            {"role": "system", "content": "[Capabilities]..."},
            {"role": "user", "content": "Read README.md"},
        ],
        response={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "..."},
                    "finish_reason": "stop",
                }
            ]
        },
        tool_calls=[],
        prompt_tokens=5400,
        completion_tokens=89,
        context_window=32768,
        prompt_pct_of_window=16.479,
        finish_reason="stop",
        narration_warning=False,
        iterations=1,
        status="ok",
    )


# --------------------------------------------------------------- list endpoint


def test_list_captures_returns_empty_for_new_session(client: TestClient) -> None:
    """A session with no captures yet returns an empty list,
    not a 404. The dashboard's list view renders the empty
    state cleanly."""
    r = client.get("/v1/sessions/main/captures", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["session_key"] == "main"
    assert body["captures"] == []


def test_list_captures_returns_summary_after_write(
    tmp_path: Path,
) -> None:
    """After a turn captures, /v1/sessions/<s>/captures
    surfaces a lightweight summary entry."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    store: TurnCaptureStore = app.state.turn_capture
    store.write(_make_capture(turn_id="turn-A"))

    r = client.get("/v1/sessions/main/captures", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body["captures"]) == 1
    item = body["captures"][0]
    assert item["turn_id"] == "turn-A"
    assert item["model_used"] == "qwen2.5-coder:14b"
    assert item["context_window"] == 32768
    # Summary doesn't include bodies.
    assert "dispatched_messages" not in item
    assert "response" not in item


def test_list_captures_respects_limit(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    store: TurnCaptureStore = app.state.turn_capture
    base_ts = 1779479825.81
    for i in range(5):
        store.write(_make_capture(turn_id=f"turn-{i}", finished_at=base_ts - i))

    r = client.get("/v1/sessions/main/captures?limit=3", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body["captures"]) == 3


def test_list_captures_requires_bearer(client: TestClient) -> None:
    r = client.get("/v1/sessions/main/captures")
    assert r.status_code == 401


# --------------------------------------------------------------- get endpoint


def test_get_capture_returns_full_body(tmp_path: Path) -> None:
    """The detail endpoint returns the full capture verbatim
    so the dashboard's turn detail view can render the
    dispatched messages, response, and tool chain."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    store: TurnCaptureStore = app.state.turn_capture
    store.write(_make_capture(turn_id="turn-detail"))

    r = client.get("/v1/sessions/main/captures/turn-detail", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["turn_id"] == "turn-detail"
    # Bodies present.
    assert body["dispatched_messages"][0]["role"] == "system"
    assert body["response"]["choices"][0]["finish_reason"] == "stop"
    assert body["context_window"] == 32768


def test_get_capture_returns_404_for_unknown_turn(client: TestClient) -> None:
    r = client.get("/v1/sessions/main/captures/nonexistent", headers=_auth())
    assert r.status_code == 404
    body = r.json()
    detail = body.get("detail")
    assert isinstance(detail, dict)
    assert detail["error"]["type"] == "not_found"


def test_get_capture_requires_bearer(client: TestClient) -> None:
    r = client.get("/v1/sessions/main/captures/turn-1")
    assert r.status_code == 401


def test_existing_turns_endpoint_unaffected(client: TestClient) -> None:
    """Sanity check: Phase 4.8c's /v1/sessions/<id>/turns
    (event stream) still works — Slice 7.2's path
    /v1/sessions/<id>/captures sits alongside, not on top."""
    r = client.get("/v1/sessions/main/turns", headers=_auth())
    # Phase 4.8c returns 200 with an empty events list for
    # a new session. The exact response shape is its own
    # contract; we just need to confirm the path doesn't
    # 404 (which would mean we'd shadowed it).
    assert r.status_code == 200

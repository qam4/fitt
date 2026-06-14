"""Tests for the gateway record/replay capture hook (Phase 12 task 3, B2).

When ``FITT_RECORD_CASSETTE`` is set, the chat path dispatches through a
shared RecordingRouter and ``POST /v1/internal/record-flush`` writes the
cassette. These tests pin: the env var builds the recorder, a real tool
turn's dispatches get captured + flushed to a replayable cassette, and
flush is a clean no-op when capture isn't enabled.

The hook sits at the router seam, so it captures *any* dispatch the chat
path makes — a plain tool turn (tested here) or a full orchestrated turn
(the operator's actual capture target) alike.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.projects import Project
from gateway.record_replay import Cassette, ReplayRouter

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import stub_reply, stub_sequence, stub_tool_call


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def test_env_var_builds_recording_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cassette = tmp_path / "cap.json"
    monkeypatch.setenv("FITT_RECORD_CASSETTE", str(cassette))
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    assert app.state.recording_router is not None
    assert app.state.record_cassette_path == cassette


def test_no_env_var_leaves_capture_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FITT_RECORD_CASSETTE", raising=False)
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    assert app.state.recording_router is None
    assert app.state.record_cassette_path is None


def test_flush_when_not_recording_returns_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FITT_RECORD_CASSETTE", raising=False)
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    client = TestClient(create_app(cfg))
    r = client.post("/v1/internal/record-flush", headers=_auth())
    assert r.status_code == 200
    assert r.json()["error"]["type"] == "not_recording"


def test_chat_turn_is_captured_and_flushed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cassette = tmp_path / "cap.json"
    monkeypatch.setenv("FITT_RECORD_CASSETTE", str(cassette))
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hi\nthe readme.\n", encoding="utf-8")
    app.state.project_registry.add(
        Project(name="hub", ssh_host="", path=str(repo), test_command="pytest -q")
    )
    client = TestClient(app)

    # A two-round tool turn: read_file, then a final reply. Both
    # dispatches go through the RecordingRouter.
    monkeypatch.setattr(
        "gateway.router.litellm.acompletion",
        stub_sequence(
            [
                stub_tool_call("read_file", {"project": "hub", "path": "README.md"}),
                stub_reply("the readme says hi"),
            ]
        ),
    )

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "read the readme"}],
            "tool_choice": "auto",
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text

    flush = client.post("/v1/internal/record-flush", headers=_auth())
    assert flush.status_code == 200, flush.text
    body = flush.json()
    assert body["interactions"] >= 2  # tool-call dispatch + final reply
    assert cassette.exists()

    # The captured cassette loads and is replayable.
    cass = Cassette.load(cassette)
    assert len(cass.interactions) >= 2
    ReplayRouter(cass)


def test_record_flush_requires_bearer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FITT_RECORD_CASSETTE", raising=False)
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    client = TestClient(create_app(cfg))
    r = client.post("/v1/internal/record-flush")
    assert r.status_code == 401

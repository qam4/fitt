"""Tests for the minimal Phase 2 session resolver."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.errors import UnknownSession

from ._fixtures import PERSONAL_TOKEN, build_test_config


def _build_client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    app = create_app(cfg)
    return TestClient(app, raise_server_exceptions=False)


def test_unknown_session_error_message_lists_available() -> None:
    e = UnknownSession("bogus", ["main"])
    assert "bogus" in str(e)
    assert "main" in str(e)


def test_chat_without_header_defaults_to_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    async def fake_acompletion(**_: Any):
        class FakeResp:
            usage = type("U", (), {"prompt_tokens": 1, "completion_tokens": 1})()

            def model_dump(self, **__: Any):
                return {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

        return FakeResp()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)
    client = _build_client(tmp_path)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": f"Bearer {PERSONAL_TOKEN}"},
    )
    assert r.status_code == 200
    assert r.headers["X-FITT-Session"] == "main"


def test_chat_rejects_unknown_session(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={
            "Authorization": f"Bearer {PERSONAL_TOKEN}",
            "X-FITT-Session": "some-other-session",
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "unknown_session"
    assert "main" in body["error"]["available"]

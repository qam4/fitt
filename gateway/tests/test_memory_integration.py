"""End-to-end tests for memory in the chat pipeline.

The headline test is ``test_keys_on_the_counter_across_restart`` -
the canonical Phase 2 validation. If that passes, memory works.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app

from ._fixtures import PERSONAL_TOKEN, build_test_config

# ---------- fake upstream -----------------------------------------


class _EchoModelResponse:
    """A fake non-streaming response whose content echoes the last
    user message. Lets the test verify what the gateway sent
    upstream without needing a real LLM."""

    def __init__(self, echoed_text: str) -> None:
        self._text = echoed_text
        self.usage = type("U", (), {"prompt_tokens": 7, "completion_tokens": 3})()

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
        }


def _install_capturing_upstream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    respond_with: str,
) -> list[dict[str, Any]]:
    """Patch litellm.acompletion to record every dispatch and return
    a predictable response. Returns the list to which captured
    kwargs are appended."""
    captured: list[dict[str, Any]] = []

    async def fake_acompletion(**kwargs: Any) -> _EchoModelResponse:
        captured.append(kwargs)
        return _EchoModelResponse(respond_with)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)
    return captured


# ---------- helpers -----------------------------------------------


def _post_chat(
    client: TestClient,
    user_text: str,
    *,
    session: str | None = None,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {PERSONAL_TOKEN}"}
    if session is not None:
        headers["X-FITT-Session"] = session
    return client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": user_text}],
        },
        headers=headers,
    )


# ---------- identity injection ------------------------------------


def test_identity_is_prepended_to_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_capturing_upstream(monkeypatch, respond_with="ok")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    # Put a distinctive marker in user.md so we can detect it
    # downstream.
    (cfg.memory.identity_dir).mkdir(parents=True, exist_ok=True)
    (cfg.memory.identity_dir / "user.md").write_text(
        "# About Me\n\nMy name is Fred.\n", encoding="utf-8"
    )
    client = TestClient(create_app(cfg))

    r = _post_chat(client, "say hi")
    assert r.status_code == 200

    # The dispatched messages list should contain a system message
    # that includes the identity marker.
    msgs = captured[0]["messages"]
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert system_msgs, "expected a system message to be injected"
    combined = "\n".join(str(m.get("content", "")) for m in system_msgs)
    assert "Fred" in combined


def test_identity_disabled_injects_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_capturing_upstream(monkeypatch, respond_with="ok")
    cfg = build_test_config(tmp_path, memory_enabled=False)
    client = TestClient(create_app(cfg))
    _post_chat(client, "say hi")
    msgs = captured[0]["messages"]
    # Memory is off so no identity/history should appear. A
    # [Capabilities] system block is injected unconditionally
    # once tools are registered (Phase 4 Task 15), and that's
    # fine — the test's actual concern is 'no memory content',
    # which we confirm by looking for the identity marker
    # instead of 'no system message at all'.
    system_msgs = [m for m in msgs if m["role"] == "system"]
    combined = "\n".join(str(m.get("content", "")) for m in system_msgs)
    assert "Fred" not in combined, "identity should not leak when memory is disabled"
    assert "# About Me" not in combined


# ---------- history injection + append ----------------------------


def test_second_turn_sees_first_turn_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_capturing_upstream(monkeypatch, respond_with="the counter")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    client = TestClient(create_app(cfg))

    _post_chat(client, "remember: my keys are on the counter")
    r2 = _post_chat(client, "where are my keys?")
    assert r2.status_code == 200

    # The second dispatch's messages should include the first turn's
    # user content as history (not just the new user message).
    second_msgs = captured[1]["messages"]
    assert any(
        m["role"] == "user" and "keys are on the counter" in str(m["content"]) for m in second_msgs
    )


def test_append_does_not_happen_on_upstream_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeUpstream500(Exception):
        status_code = 500
        message = "server error"

    async def fake_acompletion(**_: Any) -> None:
        raise FakeUpstream500("server error")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)
    cfg = build_test_config(tmp_path, memory_enabled=True)
    client = TestClient(create_app(cfg))
    r = _post_chat(client, "anything")
    assert r.status_code in (500, 502, 503)
    # No history file should have been written.
    history_file = cfg.memory.sessions_dir / "main" / "history"
    # Dir may or may not exist; contents must be empty of .md files.
    if history_file.exists():
        assert not list(history_file.glob("*.md"))


# ---------- the canonical Phase 2 test ----------------------------


def test_keys_on_the_counter_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical Phase 2 validation.

    1. Build an app, tell FITT "my keys are on the counter",
       receive a response.
    2. Destroy the app (simulating a gateway restart).
    3. Build a fresh app against the same FITT_HOME.
    4. Ask "where are my keys?" - the dispatched upstream request
       should include the counter-mention from turn 1.
    """
    captured = _install_capturing_upstream(monkeypatch, respond_with="ok")

    # Shared config between the two app instances (same on-disk
    # sessions_dir + identity_dir).
    cfg_1 = build_test_config(tmp_path, memory_enabled=True)
    app_1 = create_app(cfg_1)
    client_1 = TestClient(app_1)

    # Turn 1: seed the memory.
    r1 = _post_chat(client_1, "my keys are on the counter")
    assert r1.status_code == 200
    assert r1.headers["X-FITT-Session"] == "main"

    # Simulate gateway restart by discarding app_1 and creating a
    # fresh one. The on-disk files in tmp_path persist.
    del client_1
    del app_1

    cfg_2 = build_test_config(tmp_path, memory_enabled=True)
    assert cfg_2.memory.sessions_dir == cfg_1.memory.sessions_dir
    app_2 = create_app(cfg_2)
    client_2 = TestClient(app_2)

    # Turn 2: verify the recovered memory is injected into the
    # upstream request.
    captured.clear()
    r2 = _post_chat(client_2, "where are my keys?")
    assert r2.status_code == 200

    # The dispatched request on turn 2 must include the turn-1 user
    # content somewhere in its messages list.
    second_messages = captured[0]["messages"]
    serialised = "\n".join(str(m.get("content", "")) for m in second_messages)
    assert "counter" in serialised.lower()

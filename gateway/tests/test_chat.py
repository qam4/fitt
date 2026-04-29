"""Integration tests for POST /v1/chat/completions.

Covers Properties 5 (alias-only) and 6 (no-leak on fallback) and the
spec's chat-endpoint acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app

from ._fixtures import PERSONAL_TOKEN, build_test_config


class _FakeResponse:
    """Mimic the parts of litellm.ModelResponse that chat.py uses."""

    def __init__(self, content: str = "hi", in_tok: int = 7, out_tok: int = 3) -> None:
        self._content = content
        self.usage = type("Usage", (), {"prompt_tokens": in_tok, "completion_tokens": out_tok})()

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
        }


class _FakeStreamChunk:
    def __init__(self, delta: str) -> None:
        self._delta = delta

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": self._delta}}],
        }


async def _fake_stream(chunks: list[str]):
    for c in chunks:
        yield _FakeStreamChunk(c)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# ---------- request validation ------------------------------------


def test_chat_rejects_unknown_alias(client: TestClient) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["type"] == "unknown_alias"
    assert "fitt-default" in body["error"]["available"]


def test_chat_rejects_model_id_as_alias(client: TestClient) -> None:
    """Concrete provider/model strings get a clear 'use an alias' error."""
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "anthropic/claude-sonnet-4.5",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(),
    )
    assert r.status_code == 400


def test_chat_rejects_missing_messages(client: TestClient) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart"},
        headers=_auth(),
    )
    assert r.status_code == 422


# ---------- happy path --------------------------------------------


def test_chat_routes_cloud_alias_to_cloud(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert captured["model"] == "openrouter/anthropic/claude-sonnet-4.5"
    assert r.headers["X-FITT-Backend"] == "openrouter:anthropic/claude-sonnet-4.5"
    assert r.headers["X-FITT-Alias"] == "fitt-smart"


def test_chat_routes_ollama_alias_to_ollama(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        captured.update(kwargs)
        return _FakeResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-default", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 200
    assert captured["model"] == "ollama_chat/qwen2.5-coder:14b"
    assert captured["api_base"] == "http://laptop.tailnet:11434"


# ---------- fallback + header fidelity (Property 6) ---------------


def test_chat_primary_unreachable_falls_back_and_header_reflects_reality(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_acompletion(**kwargs: Any) -> _FakeResponse:
        if kwargs.get("api_base") == "http://laptop.tailnet:11434":
            raise httpx.ConnectError("laptop asleep")
        return _FakeResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-default", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 200
    # Property 6: header names the actual backend
    assert r.headers["X-FITT-Backend"] == "ollama:http://localhost:11434"
    assert r.headers["X-FITT-Fallback"] == "1"


def test_chat_both_unreachable_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_acompletion(**_: Any) -> None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-default", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "no_backend_available"


# ---------- upstream 429/529 translation --------------------------


def test_chat_upstream_429_returns_503_with_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Fake429(Exception):
        status_code = 429
        message = "too many requests"
        response = type("R", (), {"headers": {"retry-after": "7"}})

    async def fake_acompletion(**_: Any) -> None:
        raise Fake429("too many requests")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "7"
    assert r.json()["error"]["upstream_status"] == 429


def test_chat_upstream_529_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    class Fake529(Exception):
        status_code = 529
        message = "overloaded"

    async def fake_acompletion(**_: Any) -> None:
        raise Fake529("overloaded")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    r = client.post(
        "/v1/chat/completions",
        json={"model": "fitt-smart", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert r.status_code == 503
    assert "retry-after" in {k.lower() for k in r.headers.keys()}


# ---------- streaming passthrough ---------------------------------


def test_chat_streaming_passthrough(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**_: Any):
        return _fake_stream(["Hel", "lo", "!"])

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers=_auth(),
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())

    # Expect three data events plus [DONE]
    text = body.decode("utf-8")
    assert text.count("data: ") >= 4
    # The three content deltas appear in order
    pos_hel = text.find('"Hel"')
    pos_lo = text.find('"lo"')
    pos_bang = text.find('"!"')
    assert pos_hel != -1 < pos_lo < pos_bang
    assert "[DONE]" in text


def test_chat_stream_mid_failure_emits_error_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken_stream():
        yield _FakeStreamChunk("ok ")
        raise RuntimeError("upstream exploded")

    async def fake_acompletion(**_: Any):
        return broken_stream()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers=_auth(),
    ) as r:
        body = b"".join(r.iter_bytes())

    text = body.decode("utf-8")
    assert "[ERROR]" in text
    assert "stream_failure" in text
    assert "upstream exploded" in text

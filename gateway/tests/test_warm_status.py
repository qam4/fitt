"""Tests for the ollama warm-state helpers (VRAM residency + eviction).

Fake httpx transport — no real ollama. Covers /api/ps parsing, the
never-raises contract, and evict_others keeping the DUT."""

from __future__ import annotations

from typing import Any

import httpx

from gateway.warm_status import evict_others, list_loaded, unload

ENDPOINT = "http://localhost:11435"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------- list_loaded


async def test_list_loaded_parses_ps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3:14b", "size_vram": 9_000_000_000, "context_length": 16384},
                    {"name": "gemma4:12b-it-qat", "size_vram": 0, "context_length": 4096},
                ]
            },
        )

    async with _client(handler) as c:
        loaded = await list_loaded(ENDPOINT, client=c)
    assert [m.name for m in loaded] == ["qwen3:14b", "gemma4:12b-it-qat"]
    assert loaded[0].size_vram == 9_000_000_000
    assert loaded[0].context_length == 16384
    assert loaded[1].size_vram == 0  # fully offloaded to CPU


async def test_list_loaded_empty_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client(handler) as c:
        assert await list_loaded(ENDPOINT, client=c) == []


async def test_list_loaded_never_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with _client(handler) as c:
        assert await list_loaded(ENDPOINT, client=c) == []


# --------------------------------------------------------------- unload


async def test_unload_posts_keep_alive_zero() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"done": True})

    async with _client(handler) as c:
        ok = await unload(ENDPOINT, "qwen3:14b", client=c)
    assert ok is True
    assert seen["path"] == "/api/chat"
    assert seen["body"]["keep_alive"] == 0
    assert seen["body"]["model"] == "qwen3:14b"


async def test_unload_false_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _client(handler) as c:
        assert await unload(ENDPOINT, "x", client=c) is False


# --------------------------------------------------------------- evict_others


async def test_evict_others_keeps_dut() -> None:
    unloaded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/api/ps":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {"name": "qwen3:14b", "size_vram": 9, "context_length": 16384},
                        {"name": "hermes3:8b", "size_vram": 5, "context_length": 16384},
                        {"name": "gemma4:12b-it-qat", "size_vram": 7, "context_length": 16384},
                    ]
                },
            )
        body = json.loads(request.content)
        unloaded.append(body["model"])
        return httpx.Response(200)

    async with _client(handler) as c:
        evicted = await evict_others(ENDPOINT, "gemma4:12b-it-qat", client=c)
    assert set(evicted) == {"qwen3:14b", "hermes3:8b"}
    assert "gemma4:12b-it-qat" not in unloaded  # DUT preserved

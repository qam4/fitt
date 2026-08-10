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


# --------------------------------------------------------------- template check


def _show_handler(template: str, caps: list[str]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/show"
        return httpx.Response(200, json={"template": template, "capabilities": caps})

    return handler


async def test_template_check_flags_stub_template_mismatch() -> None:
    """The gemma4 case: claims `tools` but the template is a raw prompt
    passthrough, so tool results can never reach the model."""
    from gateway.warm_status import check_template

    async with _client(_show_handler("{{ .Prompt }}", ["completion", "tools"])) as c:
        tc = await check_template(ENDPOINT, "gemma4:12b-it-qat", client=c)
    assert tc.claims_tools is True
    assert tc.renders_messages is False
    assert tc.mentions_tools is False
    assert tc.tool_capable is False
    assert tc.mismatch is True  # the dangerous combination


async def test_template_check_passes_real_tool_template() -> None:
    """The hermes/qwen3 case: template renders messages and tool calls."""
    from gateway.warm_status import check_template

    tmpl = "{{ range .Messages }}{{ if .ToolCalls }}<tool_call>{{ end }}{{ end }}"
    async with _client(_show_handler(tmpl, ["completion", "tools"])) as c:
        tc = await check_template(ENDPOINT, "hermes3:8b", client=c)
    assert tc.tool_capable is True
    assert tc.mismatch is False


async def test_template_check_no_tool_claim_is_not_a_mismatch() -> None:
    """A model that doesn't claim tools isn't misrepresenting itself."""
    from gateway.warm_status import check_template

    async with _client(_show_handler("{{ .Prompt }}", ["completion"])) as c:
        tc = await check_template(ENDPOINT, "some-embed", client=c)
    assert tc.mismatch is False


async def test_template_check_never_raises() -> None:
    from gateway.warm_status import check_template

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with _client(handler) as c:
        tc = await check_template(ENDPOINT, "x", client=c)
    assert tc.detail is not None
    assert tc.tool_capable is False

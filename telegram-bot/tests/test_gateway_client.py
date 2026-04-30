"""Tests for the gateway HTTP client."""

from __future__ import annotations

import httpx
import respx

from fitt_telegram_bot.gateway_client import GatewayClient


def _client() -> GatewayClient:
    return GatewayClient("http://127.0.0.1:8080", "TEST_TOKEN")


async def _collect(aiter) -> list[str]:
    return [x async for x in aiter]


async def test_list_aliases_parses_response() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8080/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {"id": "fitt-default", "object": "model"},
                        {"id": "fitt-smart", "object": "model"},
                    ],
                },
            )
        )
        aliases = await _client().list_aliases()
    assert aliases == ["fitt-default", "fitt-smart"]


async def test_list_aliases_tolerates_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8080/v1/models").mock(side_effect=httpx.ConnectError("boom"))
        aliases = await _client().list_aliases()
    assert aliases == []


async def test_chat_streams_deltas() -> None:
    sse = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b"data: [DONE]\n\n"
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse, headers={"Content-Type": "text/event-stream"}
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert deltas == ["Hel", "lo"]


async def test_chat_rate_limited_surface() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                503,
                json={"error": {"message": "too busy"}},
                headers={"retry-after": "7"},
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "rate limited" in deltas[0]
    assert "7" in deltas[0]


async def test_chat_unauthorised_surface() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": "bad token"}})
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "401" in deltas[0]


async def test_chat_unreachable_yields_error_string() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            side_effect=httpx.ConnectError("nope")
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert len(deltas) == 1
    assert "unreachable" in deltas[0]


async def test_chat_upstream_stream_abort() -> None:
    sse = b"data: [ERROR]\n\n"
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8080/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, content=sse, headers={"Content-Type": "text/event-stream"}
            )
        )
        deltas = await _collect(
            _client().chat(
                messages=[{"role": "user", "content": "hi"}],
                alias="fitt-smart",
                session_id="main",
            )
        )
    assert "upstream stream aborted" in deltas[0]

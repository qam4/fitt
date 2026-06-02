"""Tests for the extracted reachability ping (Phase 7.6).

The ping logic moved out of ``health.py`` into
``gateway.reachability`` so the alias probe can run the same
check on a canary timeout. ``/ready``'s own behavior is pinned
unchanged by ``test_health.py``; these tests cover the helper
directly: per-backend reachable / unreachable / timeout, and
latency recording.

httpx calls are mocked with respx so no real network traffic.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from gateway.config import ModelConfig
from gateway.reachability import (
    ReachabilityResult,
    check_reachable,
    check_reachable_standalone,
)


def _ollama_model() -> ModelConfig:
    return ModelConfig(
        id="qwen-big",
        backend="ollama",
        endpoint="http://laptop.tailnet:11434",
        model="qwen3:14b",
    )


def _openrouter_model() -> ModelConfig:
    return ModelConfig(
        id="or-sonnet",
        backend="openrouter",
        model="anthropic/claude-sonnet-4.5",
        cost_per_mtok_in=Decimal("3"),
        cost_per_mtok_out=Decimal("15"),
    )


def _openai_model() -> ModelConfig:
    return ModelConfig(
        id="nim-deepseek",
        backend="openai",
        endpoint="https://integrate.api.nvidia.com/v1",
        model="deepseek-ai/deepseek-v4-flash",
    )


async def test_ollama_reachable_when_tags_responds() -> None:
    with respx.mock:
        respx.get("http://laptop.tailnet:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await check_reachable(client, _ollama_model())
    assert r.reachable is True
    assert r.model_id == "qwen-big"
    assert r.latency_ms >= 0


async def test_ollama_unreachable_on_connect_error() -> None:
    with respx.mock:
        respx.get("http://laptop.tailnet:11434/api/tags").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await check_reachable(client, _ollama_model())
    assert r.reachable is False
    assert r.detail is not None
    assert "refused" in r.detail


async def test_ollama_unreachable_on_5xx() -> None:
    """A 5xx means the host answered but is broken — we treat
    that as not-reachable (status_code >= 500)."""
    with respx.mock:
        respx.get("http://laptop.tailnet:11434/api/tags").mock(return_value=httpx.Response(502))
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await check_reachable(client, _ollama_model())
    assert r.reachable is False


async def test_openrouter_reachable() -> None:
    with respx.mock:
        respx.get("https://openrouter.ai/api/v1/models").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await check_reachable(client, _openrouter_model())
    assert r.reachable is True


async def test_anthropic_401_is_reachable() -> None:
    """A 401 from Anthropic's /v1/models still means the host
    answered — reachable."""
    with respx.mock:
        respx.get("https://api.anthropic.com/v1/models").mock(return_value=httpx.Response(401))
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await check_reachable(
                client,
                ModelConfig(id="claude", backend="anthropic", model="claude-sonnet-4-5"),
            )
    assert r.reachable is True


async def test_openai_backend_probes_endpoint_models() -> None:
    """Phase 7.6 adds the openai backend to the ping (it wasn't
    in /ready's original set). Probes <endpoint>/models."""
    with respx.mock:
        respx.get("https://integrate.api.nvidia.com/v1/models").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await check_reachable(client, _openai_model())
    assert r.reachable is True


async def test_standalone_wrapper_constructs_client() -> None:
    """check_reachable_standalone builds its own client — used by
    the probe, which doesn't hold one."""
    with respx.mock:
        respx.get("http://laptop.tailnet:11434/api/tags").mock(
            return_value=httpx.Response(200, json={"models": []})
        )
        r = await check_reachable_standalone(_ollama_model())
    assert r.reachable is True
    assert isinstance(r, ReachabilityResult)


@pytest.mark.parametrize("timeout", [1.0, 2.5, 5.0])
async def test_timeout_is_respected_param(timeout: float) -> None:
    """The timeout_s arg flows through; a slow endpoint past the
    budget reads as unreachable."""
    with respx.mock:
        respx.get("http://laptop.tailnet:11434/api/tags").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        r = await check_reachable_standalone(_ollama_model(), timeout_s=timeout)
    assert r.reachable is False
    assert r.detail is not None

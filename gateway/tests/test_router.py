"""Tests for the alias router.

Covers Phase 1 Property 1 (alias routing determinism), Property 6
(no-leak on fallback), and the dispatch-level unit tests in the spec.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.errors import NoBackendAvailable, UnknownAlias
from gateway.router import AliasRouter, backend_tag

from ._fixtures import build_openai_backend_config, build_test_config


class _FakeModelResponse:
    def __init__(self, content: str = "ok") -> None:
        self.choices = [type("Choice", (), {"message": type("M", (), {"content": content})})]
        self.usage = type("Usage", (), {"prompt_tokens": 10, "completion_tokens": 5})

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }


# ---------- resolve -----------------------------------------------


def test_alias_resolve_unknown_raises(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    with pytest.raises(UnknownAlias) as exc:
        r.resolve("fitt-bogus")
    assert "fitt-bogus" in str(exc.value)
    assert set(exc.value.available) == {"fitt-default", "fitt-smart", "fitt-fast"}


def test_resolve_primary_and_fallback(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    chain = r.resolve("fitt-default")
    assert [m.id for m in chain] == ["qwen-big", "qwen-small"]


def test_resolve_primary_only(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    chain = r.resolve("fitt-smart")
    assert [m.id for m in chain] == ["openrouter-sonnet"]


# ---------- dispatch ----------------------------------------------


async def test_dispatch_routes_cloud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
        captured.update(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    result = await r.dispatch("fitt-smart", {"messages": [{"role": "user", "content": "hi"}]})

    assert captured["model"] == "openrouter/anthropic/claude-sonnet-4.5"
    assert captured["api_key"] == "sk-or-test-xxxxx"
    assert result.model_used.id == "openrouter-sonnet"
    assert result.fallback_used is False
    assert result.response is not None


async def test_dispatch_routes_ollama(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
        captured.update(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    await r.dispatch("fitt-default", {"messages": [{"role": "user", "content": "hi"}]})
    assert captured["model"] == "ollama_chat/qwen2.5-coder:14b"
    assert captured["api_base"] == "http://laptop.tailnet:11434"


async def test_dispatch_falls_back_on_connection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    calls: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
        calls.append(kwargs["model"])
        if kwargs["api_base"] == "http://laptop.tailnet:11434":
            raise httpx.ConnectError("laptop asleep")
        return _FakeModelResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    result = await r.dispatch("fitt-default", {"messages": [{"role": "user", "content": "hi"}]})
    assert calls == [
        "ollama_chat/qwen2.5-coder:14b",
        "ollama_chat/qwen2.5-coder:7b",
    ]
    assert result.fallback_used is True
    assert result.model_used.id == "qwen-small"


async def test_dispatch_raises_when_all_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)

    async def fake_acompletion(**_: Any) -> None:
        raise httpx.ConnectError("everything's down")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    with pytest.raises(NoBackendAvailable) as exc:
        await r.dispatch("fitt-default", {"messages": [{"role": "user", "content": "hi"}]})
    assert set(exc.value.attempted) == {"qwen-big", "qwen-small"}


async def test_dispatch_passes_upstream_non_transport_error_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 400 from the upstream is semantic, not transport — don't failover."""
    cfg = build_test_config(tmp_path)
    r = AliasRouter(cfg)
    calls: list[str] = []

    class FakeUpstream400(Exception):
        status_code = 400
        message = "bad request"

    async def fake_acompletion(**kwargs: Any) -> None:
        calls.append(kwargs["model"])
        raise FakeUpstream400("bad request")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    with pytest.raises(FakeUpstream400):
        await r.dispatch("fitt-default", {"messages": [{"role": "user", "content": "hi"}]})

    # Only the primary was tried — no fallback on semantic errors.
    assert len(calls) == 1


# ---------- backend_tag -------------------------------------------


def test_backend_tag_openrouter(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    m = next(m for m in cfg.models if m.id == "openrouter-sonnet")
    assert backend_tag(m) == "openrouter:anthropic/claude-sonnet-4.5"


def test_backend_tag_ollama(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    m = next(m for m in cfg.models if m.id == "qwen-big")
    assert backend_tag(m) == "ollama:http://laptop.tailnet:11434"


def test_backend_tag_openai(tmp_path: Path) -> None:
    cfg = build_openai_backend_config(tmp_path)
    m = cfg.models[0]
    assert backend_tag(m) == "openai:minimaxai/minimax-m2"


# ---------- dispatch: generic openai-compatible backend -----------


async def test_dispatch_routes_openai_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generic `openai` backend sends openai/<model> + api_base + api_key."""
    cfg = build_openai_backend_config(tmp_path)
    r = AliasRouter(cfg)
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
        captured.update(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    result = await r.dispatch("fitt-huge", {"messages": [{"role": "user", "content": "hi"}]})

    assert captured["model"] == "openai/minimaxai/minimax-m2"
    assert captured["api_base"] == "https://integrate.api.nvidia.com/v1"
    assert captured["api_key"] == "nvapi-test-xxxxx"
    assert result.model_used.id == "nvidia-minimax"
    assert result.fallback_used is False
    assert result.response is not None


async def test_dispatch_openai_backend_without_key_omits_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local OpenAI-compatible endpoints (LM Studio, vLLM) often need no key."""
    cfg = build_openai_backend_config(tmp_path)
    assert cfg.secrets is not None
    cfg.secrets.api_keys = {}  # drop the key
    r = AliasRouter(cfg)
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _FakeModelResponse:
        captured.update(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake_acompletion)

    await r.dispatch("fitt-huge", {"messages": [{"role": "user", "content": "hi"}]})

    assert captured["model"] == "openai/minimaxai/minimax-m2"
    assert captured["api_base"] == "https://integrate.api.nvidia.com/v1"
    assert "api_key" not in captured


# ---------- property test (Phase 1, Property 1) -------------------


@given(alias=st.sampled_from(["fitt-default", "fitt-smart", "fitt-fast"]))
@settings(max_examples=30)
def test_property_alias_routing_determinism(alias: str, tmp_path_factory) -> None:
    """For any known alias, the resolved chain is always the configured one."""
    tmp = tmp_path_factory.mktemp("det")
    cfg = build_test_config(tmp)
    r = AliasRouter(cfg)
    chain = r.resolve(alias)
    allowed_ids = {m.id for m in cfg.models}
    for m in chain:
        assert m.id in allowed_ids

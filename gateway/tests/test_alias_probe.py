"""Tests for :mod:`gateway.alias_probe` — the boot-time
tool-call reliability check.

Three concerns:

* Shape-level classification: a real ``tool_calls`` response →
  ``ok``; a text-only narrated response → ``narrated``; a
  length-cutoff response → ``truncated``; an empty reply with no
  tool_calls → ``empty_reply``.
* Failure taxonomy (Phase 7.6): a dispatch exception is
  classified via the shared :mod:`gateway.dispatch_outcome`
  vocabulary (``upstream_server_error`` for a bare transport
  failure, etc.); a *timeout* runs a reachability ping and
  resolves to ``upstream_silent`` (host reachable, model slow /
  cold-loading) or ``unreachable`` (host down). Latency is
  recorded on every path.
* Batch behaviour: multiple aliases probe concurrently;
  aliases missing an api_keys entry skip cleanly with
  ``skipped_no_api_key`` and don't incur a dispatch.
* Regression-grade: a sentinel-narrated reply (like qwen3-next
  did on 2026-05-10) is classified as ``narrated``, not ``ok``,
  even though the text looks JSON-ish.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gateway.alias_probe import (
    ProbeResult,
    probe_alias,
    probe_all_aliases,
)
from gateway.config import (
    AllowedToken,
    Config,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    Secrets,
    ServerConfig,
)
from gateway.router import DispatchResult

# --------------------------------------------------------------- scaffolding


def _cfg(tmp_path: Path, *, has_key: bool = True) -> Config:
    """Two-alias config: one openai-backend (needs a key) and
    one ollama (doesn't). Lets one test exercise both skip and
    live paths from the same config."""
    fitt_home = tmp_path / "fitt"
    fitt_home.mkdir(exist_ok=True)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080),
        aliases={"fitt-smart": "nim-qwen", "fitt-default": "local-ollama"},
        models=[
            ModelConfig(
                id="nim-qwen",
                backend="openai",
                endpoint="https://integrate.api.nvidia.com/v1",
                model="qwen/qwen3-next-80b-a3b-instruct",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
            ModelConfig(
                id="local-ollama",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="qwen2.5-coder:14b",
            ),
        ],
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
        memory=MemoryConfig(
            enabled=False,
            identity_dir=fitt_home / "identity",
            sessions_dir=fitt_home / "sessions",
        ),
    )
    api_keys = {"nim-qwen": "nvapi-test-xxx"} if has_key else {}
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
        api_keys=api_keys,
    )
    return cfg


def _make_response(
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    content: str | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """Minimal OpenAI response shape mirroring what LiteLLM
    returns. ``extract_tool_calls`` treats a missing
    ``finish_reason`` as ``"tool_calls"`` when tool_calls are
    present; we mirror that contract so the test doubles are
    indistinguishable from live dispatch."""
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    else:
        msg["content"] = content or ""
    return {
        "id": "resp-1",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": ("tool_calls" if tool_calls is not None else finish_reason),
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }


class _StubRouter:
    """A drop-in for AliasRouter.dispatch. Tests pin the
    response per-alias via ``set``; tests that want a failure
    set a callable that raises."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._responses: dict[str, Any] = {}

    def set(self, alias: str, response_or_exc: Any) -> None:
        self._responses[alias] = response_or_exc

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        del body
        r = self._responses.get(alias)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return await r()
        # Pick the primary model from the config so the
        # DispatchResult looks realistic.
        primary = self._config.resolve_alias(alias)[0]
        return DispatchResult(
            response=r,
            stream=None,
            model_used=primary,
            fallback_used=False,
        )


# --------------------------------------------------------------- ok path


async def test_probe_ok_when_real_tool_calls_emitted(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set(
        "fitt-smart",
        _make_response(
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "_fitt_probe", "arguments": "{}"},
                }
            ]
        ),
    )

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    assert result.status == "ok"
    assert result.model_used == "nim-qwen"
    assert "1 tool call" in result.detail
    assert result.latency_ms >= 0


# --------------------------------------------------------------- narration


async def test_probe_narrated_on_text_only_reply(tmp_path: Path) -> None:
    """The production failure from 2026-05-07: model emits a
    polite text reply and no tool_calls. Shape-level: tools
    were offered, no real call, long reply → narrated."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set(
        "fitt-smart",
        _make_response(
            content=(
                "Sure, I'll call the _fitt_probe tool for you now! "
                "The probe has been executed successfully and "
                "everything looks good on my end."
            )
        ),
    )

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    assert result.status == "narrated"
    assert "reply" in result.detail
    assert "_fitt_probe" in result.reply_preview
    assert result.finish_reason == "stop"


async def test_probe_narrated_on_sentinel_shape(tmp_path: Path) -> None:
    """Regression for the 2026-05-10 qwen3-next failure: the
    sentinel-narrated reply looks JSON-ish in content but there
    are no actual tool_calls. The probe must still classify it
    as narrated — the signal is the shape, not the content."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set(
        "fitt-smart",
        _make_response(
            content=("TOOL_NAME: _fitt_probe BEGIN_ARG: {} END_ARG: The tool has been called.")
        ),
    )

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    assert result.status == "narrated"


async def test_probe_ignores_short_polite_ack_as_not_narration(
    tmp_path: Path,
) -> None:
    """A one-word "ok" is ambiguous — could be a polite ack
    after the model decided not to call, could be truncation.
    We deliberately don't count short replies as narration so
    the probe stays low-false-positive."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set("fitt-smart", _make_response(content="ok"))

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    # Neither narrated (too short) nor ok (no tool_calls). The
    # dispatch *succeeded* — the model just produced nothing
    # useful — so this is the model-behavior ``empty_reply``
    # anomaly, not a transport failure (Phase 7.6).
    assert result.status == "empty_reply"
    assert "empty reply" in result.detail


# --------------------------------------------------------------- truncation


async def test_probe_detects_length_cutoff(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set(
        "fitt-smart",
        _make_response(
            content="Here is what I will do next: call the",
            finish_reason="length",
        ),
    )

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    assert result.status == "truncated"
    assert result.finish_reason == "length"


# --------------------------------------------------------------- dispatch failure


async def test_probe_catches_dispatch_exception(tmp_path: Path) -> None:
    """A bare (non-HTTP) dispatch exception classifies via the
    shared taxonomy as ``upstream_server_error`` — the catch-all
    bucket for transport failures with no status code."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set("fitt-smart", RuntimeError("connection refused"))

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    assert result.status == "upstream_server_error"
    assert "connection refused" in result.detail
    assert "RuntimeError" in result.detail
    assert result.latency_ms >= 0


async def test_probe_times_out_reachable_is_upstream_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout whose endpoint still answers a reachability
    ping means the model is slow / cold-loading, not down. The
    probe must report ``upstream_silent`` (Phase 7.6) — the
    exact 2026-05-28 VRAM-contention incident."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)

    async def slow() -> DispatchResult:
        await asyncio.sleep(10.0)
        return DispatchResult(None, None, cfg.models[0], False)

    router.set("fitt-smart", slow)

    from gateway import alias_probe
    from gateway.reachability import ReachabilityResult

    async def fake_reachable(model: Any, **_: Any) -> ReachabilityResult:
        return ReachabilityResult(model.id, True, 42)

    monkeypatch.setattr(alias_probe, "check_reachable_standalone", fake_reachable)

    result = await probe_alias(
        "fitt-smart",  # type: ignore[arg-type]
        router,
        timeout_s=0.05,
        config=cfg,
    )
    assert result.status == "upstream_silent"
    assert "reachable" in result.detail
    assert result.reachable is True
    assert result.latency_ms >= 0


async def test_probe_times_out_unreachable_is_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout whose endpoint also fails the reachability ping
    means the host is genuinely down → ``unreachable``."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)

    async def slow() -> DispatchResult:
        await asyncio.sleep(10.0)
        return DispatchResult(None, None, cfg.models[0], False)

    router.set("fitt-smart", slow)

    from gateway import alias_probe
    from gateway.reachability import ReachabilityResult

    async def fake_unreachable(model: Any, **_: Any) -> ReachabilityResult:
        return ReachabilityResult(model.id, False, 2500, detail="connect timeout")

    monkeypatch.setattr(alias_probe, "check_reachable_standalone", fake_unreachable)

    result = await probe_alias(
        "fitt-smart",  # type: ignore[arg-type]
        router,
        timeout_s=0.05,
        config=cfg,
    )
    assert result.status == "unreachable"
    assert "unreachable" in result.detail
    assert result.reachable is False


async def test_probe_times_out_without_config_falls_back_silent(
    tmp_path: Path,
) -> None:
    """With no ``config`` to resolve the model, a timeout can't
    run the disambiguating ping, so it falls back to the
    conservative ``upstream_silent`` (a timeout most often means
    slow, not down)."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)

    async def slow() -> DispatchResult:
        await asyncio.sleep(10.0)
        return DispatchResult(None, None, cfg.models[0], False)

    router.set("fitt-smart", slow)
    result = await probe_alias(
        "fitt-smart",  # type: ignore[arg-type]
        router,
        timeout_s=0.05,
    )
    assert result.status == "upstream_silent"
    assert "timed out" in result.detail
    assert "reachability not checked" in result.detail


# --------------------------------------------------------------- batch


async def test_probe_all_aliases_runs_concurrently(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set(
        "fitt-smart",
        _make_response(
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "_fitt_probe", "arguments": "{}"},
                }
            ]
        ),
    )
    router.set(
        "fitt-default",
        _make_response(
            content=(
                "I can't do that. I don't have access to any tools "
                "that would let me call _fitt_probe."
            )
        ),
    )

    results = await probe_all_aliases(cfg, router)  # type: ignore[arg-type]
    by_alias = {r.alias: r for r in results}
    assert by_alias["fitt-smart"].status == "ok"
    assert by_alias["fitt-default"].status == "narrated"


async def test_probe_all_aliases_serializes_same_endpoint(tmp_path: Path) -> None:
    """Property 3: aliases that resolve to the *same* endpoint
    are probed one at a time (no two canaries in-flight at once
    for that endpoint), while distinct endpoints may overlap.

    This is the fix for the 2026-05-28 VRAM-contention incident.
    We build three aliases on one Ollama endpoint and one alias
    on a different endpoint, then track concurrency per endpoint
    via an instrumented dispatch."""
    fitt_home = tmp_path / "fitt"
    fitt_home.mkdir(exist_ok=True)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080),
        aliases={
            "a1": "ollama-a",
            "a2": "ollama-a",
            "a3": "ollama-a",
            "b1": "ollama-b",
        },
        models=[
            ModelConfig(id="ollama-a", backend="ollama", endpoint="http://laptop:11434", model="m"),
            ModelConfig(id="ollama-b", backend="ollama", endpoint="http://hub:11434", model="m"),
        ],
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
        memory=MemoryConfig(
            enabled=False,
            identity_dir=fitt_home / "identity",
            sessions_dir=fitt_home / "sessions",
        ),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
        api_keys={},
    )

    # Track max concurrent in-flight dispatches per endpoint.
    in_flight: dict[str, int] = {}
    max_seen: dict[str, int] = {}
    lock = asyncio.Lock()

    ok_response = _make_response(
        tool_calls=[
            {"id": "c", "type": "function", "function": {"name": "_fitt_probe", "arguments": "{}"}}
        ]
    )

    class _ConcurrencyRouter:
        async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
            del body
            primary = cfg.resolve_alias(alias)[0]
            ep = primary.endpoint or primary.id
            async with lock:
                in_flight[ep] = in_flight.get(ep, 0) + 1
                max_seen[ep] = max(max_seen.get(ep, 0), in_flight[ep])
            try:
                await asyncio.sleep(0.02)  # hold the "GPU"
            finally:
                async with lock:
                    in_flight[ep] -= 1
            return DispatchResult(
                response=ok_response, stream=None, model_used=primary, fallback_used=False
            )

    results = await probe_all_aliases(cfg, _ConcurrencyRouter())  # type: ignore[arg-type]
    assert all(r.status == "ok" for r in results)
    # The three same-endpoint aliases never overlapped.
    assert max_seen["http://laptop:11434"] == 1
    # Results come back in config order regardless of grouping.
    assert [r.alias for r in results] == ["a1", "a2", "a3", "b1"]


async def test_probe_all_aliases_skips_when_api_key_missing(
    tmp_path: Path,
) -> None:
    """The api_keys check already logged this misconfiguration;
    re-probing just produces a duplicate dispatch failure. The
    probe must skip cleanly."""
    cfg = _cfg(tmp_path, has_key=False)
    router = _StubRouter(cfg)
    # If we accidentally dispatch, this would raise — assertion
    # failure rather than silent pass.
    router.set("fitt-smart", RuntimeError("should not be probed"))
    # Ollama alias doesn't need a key; it can probe cleanly.
    router.set(
        "fitt-default",
        _make_response(
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "_fitt_probe", "arguments": "{}"},
                }
            ]
        ),
    )

    results = await probe_all_aliases(cfg, router)  # type: ignore[arg-type]
    by_alias = {r.alias: r for r in results}
    assert by_alias["fitt-smart"].status == "skipped_no_api_key"
    assert "api_keys.nim-qwen" in by_alias["fitt-smart"].detail
    assert by_alias["fitt-default"].status == "ok"


async def test_probe_result_is_frozen_dataclass() -> None:
    """Pin the immutability contract: probe results flow
    through log formatting and structured output, so a future
    refactor must not accidentally mutate one after the fact."""
    from dataclasses import FrozenInstanceError

    r = ProbeResult(alias="x", status="ok", detail="ok")
    with pytest.raises(FrozenInstanceError):
        r.alias = "y"  # type: ignore[misc]

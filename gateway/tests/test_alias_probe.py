"""Tests for :mod:`gateway.alias_probe` — the boot-time
tool-call reliability check.

Three concerns:

* Shape-level classification: a real ``tool_calls`` response →
  ``ok``; a text-only narrated response → ``narrated``; a
  length-cutoff response → ``truncated``; transport/timeout →
  ``transport_error``.
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
    # Neither narrated (too short) nor ok (no tool_calls) — we
    # classify the anomaly as transport_error so the log line
    # makes sense. An operator debugging this gets enough
    # signal from the status to dig deeper.
    assert result.status == "transport_error"
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


# --------------------------------------------------------------- transport


async def test_probe_catches_dispatch_exception(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)
    router.set("fitt-smart", RuntimeError("connection refused"))

    result = await probe_alias("fitt-smart", router)  # type: ignore[arg-type]
    assert result.status == "transport_error"
    assert "connection refused" in result.detail
    assert "RuntimeError" in result.detail


async def test_probe_times_out(tmp_path: Path) -> None:
    """If the dispatch never returns inside the timeout, the
    probe should return transport_error rather than block
    gateway startup forever."""
    cfg = _cfg(tmp_path)
    router = _StubRouter(cfg)

    async def slow() -> DispatchResult:
        await asyncio.sleep(10.0)
        # Never reached.
        return DispatchResult(None, None, cfg.models[0], False)

    router.set("fitt-smart", slow)
    result = await probe_alias(
        "fitt-smart",  # type: ignore[arg-type]
        router,
        timeout_s=0.05,
    )
    assert result.status == "transport_error"
    assert "timed out" in result.detail


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


async def test_probe_all_aliases_skips_when_api_key_missing(
    tmp_path: Path,
) -> None:
    """The api_keys check already logged this misconfiguration;
    re-probing just produces a duplicate transport failure. The
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

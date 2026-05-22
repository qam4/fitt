"""Tests for :mod:`gateway.context_window` — Phase 7 Slice 7.1.

Per-backend probes plus the cache layer. Three concerns:

* Each probe's happy path lifts the right field with the right
  ``source`` provenance.
* Each probe's failure modes (auth, transport, malformed
  response, missing field) degrade to ``tokens=None`` /
  ``source="unknown"`` rather than raising.
* The cache populates concurrently, exposes results via
  ``get`` / ``all_results``, and refreshes individual
  bindings without re-running every probe.

The probes never raise. Every test that asserts a failure
asserts on the result shape, not on a propagated exception.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from gateway.config import (
    AllowedToken,
    Config,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    Secrets,
    ServerConfig,
)
from gateway.context_window import (
    AnthropicContextProbe,
    ContextWindowCache,
    ContextWindowResult,
    OllamaContextProbe,
    OpenAIContextProbe,
)

# --------------------------------------------------------------- helpers


def _cfg(
    tmp_path: Path, models: list[ModelConfig], *, api_keys: dict[str, str] | None = None
) -> Config:
    """Build a Config with the given models and a sane secrets layer.

    Each model gets a dummy alias so :class:`Config`'s validator
    doesn't reject the graph (every model needs at least one
    pointer to be reachable)."""
    fitt_home = tmp_path / "fitt"
    fitt_home.mkdir(exist_ok=True)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080, boot_probe_enabled=False),
        aliases={f"alias-{m.id}": m.id for m in models},
        models=models,
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
        memory=MemoryConfig(
            enabled=False,
            identity_dir=fitt_home / "identity",
            sessions_dir=fitt_home / "sessions",
        ),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
        api_keys=api_keys or {},
    )
    return cfg


def _ollama_show_response(
    *,
    parameters: str | None = None,
    architecture: str | None = None,
    architecture_context_length: int | None = None,
) -> dict[str, Any]:
    """Build a synthetic ``/api/show`` response."""
    body: dict[str, Any] = {}
    if parameters is not None:
        body["parameters"] = parameters
    model_info: dict[str, Any] = {}
    if architecture is not None:
        model_info["general.architecture"] = architecture
        if architecture_context_length is not None:
            model_info[f"{architecture}.context_length"] = architecture_context_length
    if model_info:
        body["model_info"] = model_info
    return body


def _ollama_model(model_id: str = "local", model: str = "granite3.3:8b") -> ModelConfig:
    return ModelConfig(
        id=model_id,
        backend="ollama",
        endpoint="http://localhost:11434",
        model=model,
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )


# --------------------------------------------------------------- ollama probe


async def test_ollama_probe_prefers_modelfile_num_ctx() -> None:
    """When ``num_ctx`` is set in the modelfile, that value is
    the discovered ceiling — even if the architecture's
    natural context length is larger. The modelfile is what
    Ollama actually uses at inference time."""
    probe = OllamaContextProbe()
    model = _ollama_model()
    body = _ollama_show_response(
        parameters='num_ctx 32768\nstop "<|endoftext|>"',
        architecture="qwen2",
        architecture_context_length=131072,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens == 32768
    assert result.source == "modelfile"


async def test_ollama_probe_falls_back_to_model_info() -> None:
    """When no num_ctx is set, the architecture's natural
    context_length is the next-best signal."""
    probe = OllamaContextProbe()
    model = _ollama_model()
    body = _ollama_show_response(
        parameters='stop "<|endoftext|>"',
        architecture="qwen2",
        architecture_context_length=131072,
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens == 131072
    assert result.source == "model_info"


async def test_ollama_probe_falls_back_to_default_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When neither num_ctx nor architecture context_length is
    available, Ollama's documented 2048 default applies. This
    is the granite-style "operator forgot to set
    OLLAMA_CONTEXT_LENGTH" case, so the probe logs WARNING."""
    probe = OllamaContextProbe()
    model = _ollama_model()
    body: dict[str, Any] = {"parameters": 'stop "<|endoftext|>"'}
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        with caplog.at_level("WARNING"):
            result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens == 2048
    assert result.source == "default"
    assert any(
        rec.message == "context_window.ollama_default_fallback"
        or "ollama_default_fallback" in rec.message
        for rec in caplog.records
    )


async def test_ollama_probe_handles_transport_error() -> None:
    """Connection failures degrade to tokens=None / source=unknown."""
    probe = OllamaContextProbe()
    model = _ollama_model()
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"
    assert "ConnectError" in result.detail


async def test_ollama_probe_handles_malformed_json() -> None:
    """Non-JSON or empty responses degrade cleanly."""
    probe = OllamaContextProbe()
    model = _ollama_model()
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, text="not json"),
        )
        result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"


async def test_ollama_probe_handles_missing_endpoint() -> None:
    """An ollama model without an endpoint can't be probed.

    Note: Config-level validation should catch this, but the
    probe is the second line of defence — it fails loud rather
    than crashing on ``None.rstrip``."""
    probe = OllamaContextProbe()
    # Bypass ModelConfig's validator by constructing without
    # the validator path — we want to assert the probe handles
    # the case even if it ever reaches it.
    model = ModelConfig.model_construct(
        id="bad",
        backend="ollama",
        model="x",
        endpoint=None,
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"
    assert "endpoint" in result.detail


# --------------------------------------------------------------- openai probe


async def test_openai_probe_reads_context_length_field() -> None:
    """Generic openai-shape backends expose ``context_length`` on
    each ``/v1/models`` entry. NIM, Groq, Together all use this."""
    probe = OpenAIContextProbe("openai")
    model = ModelConfig(
        id="nim-qwen",
        backend="openai",
        endpoint="https://integrate.api.nvidia.com/v1",
        model="qwen/qwen3-next-80b-a3b-instruct",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
        api_keys={"nim-qwen": "nvapi-xxx"},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://integrate.api.nvidia.com/v1/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "qwen/qwen3-next-80b-a3b-instruct",
                            "object": "model",
                            "context_length": 131072,
                        },
                        {
                            "id": "other/model",
                            "object": "model",
                            "context_length": 8192,
                        },
                    ],
                },
            ),
        )
        result = await probe.discover(model, secrets, timeout_s=5.0)
    assert result.tokens == 131072
    assert result.source == "api_models"
    assert "qwen/qwen3-next-80b-a3b-instruct" in result.detail


async def test_openai_probe_falls_back_to_max_input_tokens() -> None:
    """OpenRouter uses ``max_input_tokens`` instead of
    ``context_length``. The probe checks both."""
    probe = OpenAIContextProbe("openrouter")
    model = ModelConfig(
        id="claude-via-or",
        backend="openrouter",
        endpoint=None,
        model="anthropic/claude-sonnet-4.5",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
        openrouter_api_key="or-xxx",
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://openrouter.ai/api/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "anthropic/claude-sonnet-4.5",
                            "max_input_tokens": 200000,
                        },
                    ],
                },
            ),
        )
        result = await probe.discover(model, secrets, timeout_s=5.0)
    assert result.tokens == 200000
    assert result.source == "api_models"


async def test_openai_probe_handles_model_not_in_list() -> None:
    """When ``/v1/models`` doesn't contain our configured model,
    the probe degrades cleanly rather than guessing."""
    probe = OpenAIContextProbe("openai")
    model = ModelConfig(
        id="missing",
        backend="openai",
        endpoint="https://example.com/v1",
        model="missing/model",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
        api_keys={"missing": "k"},
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/v1/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"object": "list", "data": [{"id": "other"}]},
            ),
        )
        result = await probe.discover(model, secrets, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"
    assert "not in /v1/models" in result.detail


async def test_openai_probe_handles_auth_failure() -> None:
    """A 401 from the upstream produces a clean unknown
    result. The api_keys check at boot already logged the
    missing-key case; we don't double-shout."""
    probe = OpenAIContextProbe("openai")
    model = ModelConfig(
        id="needs-key",
        backend="openai",
        endpoint="https://example.com/v1",
        model="x",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token="T" * 32)],
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/v1/v1/models").mock(
            return_value=httpx.Response(401, json={"error": "no key"}),
        )
        result = await probe.discover(model, secrets, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"


async def test_openai_probe_missing_context_field() -> None:
    """Some providers return ``/v1/models`` entries without any
    context length field. Bedrock historically didn't expose
    one. Result is unknown, not a guess."""
    probe = OpenAIContextProbe("openai")
    model = ModelConfig(
        id="silent",
        backend="openai",
        endpoint="https://example.com/v1",
        model="silent/model",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://example.com/v1/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": "silent/model"}],
                },
            ),
        )
        result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"
    assert "missing context_length" in result.detail


# --------------------------------------------------------------- anthropic probe


async def test_anthropic_probe_uses_lookup_table() -> None:
    """Anthropic doesn't expose context length on any API; the
    static lookup table is the answer. claude-sonnet-4-5
    matches its family entry."""
    probe = AnthropicContextProbe()
    model = ModelConfig(
        id="claude",
        backend="anthropic",
        endpoint=None,
        model="claude-sonnet-4-5",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens == 200000
    assert result.source == "lookup_table"
    assert "claude-sonnet-4-5" in result.detail


async def test_anthropic_probe_unknown_family_returns_unknown() -> None:
    """A model not in the lookup table degrades to unknown
    with a hint to add the family. New Anthropic generations
    will hit this until someone updates the table."""
    probe = AnthropicContextProbe()
    model = ModelConfig(
        id="future",
        backend="anthropic",
        endpoint=None,
        model="claude-quintuple-5",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )
    result = await probe.discover(model, None, timeout_s=5.0)
    assert result.tokens is None
    assert result.source == "unknown"
    assert "_ANTHROPIC_CONTEXT_BY_FAMILY" in result.detail


# --------------------------------------------------------------- cache


async def test_cache_populate_runs_per_backend(tmp_path: Path) -> None:
    """populate() probes every model in the config concurrently
    and stores one result per (backend, model_id) pair."""
    cfg = _cfg(
        tmp_path,
        [
            _ollama_model("ollama-1", "model-a"),
            ModelConfig(
                id="anthropic-1",
                backend="anthropic",
                endpoint=None,
                model="claude-sonnet-4-5",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
        ],
    )
    body = _ollama_show_response(parameters="num_ctx 8192")
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        cache = ContextWindowCache()
        await cache.populate(cfg, timeout_s=2.0)

    ollama = cache.get("ollama", "ollama-1")
    assert ollama is not None
    assert ollama.tokens == 8192
    assert ollama.source == "modelfile"

    anth = cache.get("anthropic", "anthropic-1")
    assert anth is not None
    assert anth.tokens == 200000
    assert anth.source == "lookup_table"


async def test_cache_populate_survives_probe_failure(tmp_path: Path) -> None:
    """One failing probe doesn't stop the others. Each binding
    gets a result regardless; the failed one stores as
    unknown."""
    cfg = _cfg(
        tmp_path,
        [
            _ollama_model("ollama-1", "model-a"),
            _ollama_model("ollama-2", "model-b"),
        ],
    )
    body = _ollama_show_response(parameters="num_ctx 4096")

    def _route(req: httpx.Request) -> httpx.Response:
        # First model succeeds; second model raises a transport
        # error.
        if "model-a" in req.read().decode():
            return httpx.Response(200, json=body)
        raise httpx.ConnectError("boom")

    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(side_effect=_route)
        cache = ContextWindowCache()
        await cache.populate(cfg, timeout_s=2.0)

    a = cache.get("ollama", "ollama-1")
    b = cache.get("ollama", "ollama-2")
    assert a is not None and a.tokens == 4096
    assert b is not None and b.tokens is None
    assert b.source == "unknown"


async def test_cache_get_returns_none_for_unknown_binding(tmp_path: Path) -> None:
    """Cache lookups for never-discovered bindings return
    None rather than raising. Consumers (``/v1/aliases``,
    ``/model``) treat None as "unknown" in their UI."""
    cache = ContextWindowCache()
    # populate() never called.
    assert cache.get("ollama", "local") is None
    assert cache.get("anthropic", "missing") is None


async def test_cache_refresh_one_updates_only_target(tmp_path: Path) -> None:
    """refresh_one re-runs discovery for one model without
    touching the others. The CLI ``fitt context refresh
    --alias <name>`` flow."""
    cfg = _cfg(
        tmp_path,
        [
            _ollama_model("a", "model-a"),
            _ollama_model("b", "model-b"),
        ],
    )

    # First populate establishes both.
    initial = _ollama_show_response(parameters="num_ctx 4096")
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=initial),
        )
        cache = ContextWindowCache()
        await cache.populate(cfg, timeout_s=2.0)

    a_before = cache.get("ollama", "a")
    b_before = cache.get("ollama", "b")
    assert a_before is not None and a_before.tokens == 4096
    assert b_before is not None and b_before.tokens == 4096

    # Refresh only ``a``; backend now reports a different value.
    refreshed = _ollama_show_response(parameters="num_ctx 32768")
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=refreshed),
        )
        result = await cache.refresh_one(cfg, "a", timeout_s=2.0)
    assert result.tokens == 32768

    # ``a`` updated; ``b`` unchanged.
    a_after = cache.get("ollama", "a")
    b_after = cache.get("ollama", "b")
    assert a_after is not None and a_after.tokens == 32768
    assert b_after is not None and b_after.tokens == 4096


async def test_cache_refresh_one_raises_for_unknown_model(tmp_path: Path) -> None:
    """refresh_one for a model that isn't in the config raises
    KeyError. CLI / endpoint translates to a 404 / clear
    message."""
    cfg = _cfg(tmp_path, [_ollama_model()])
    cache = ContextWindowCache()
    with pytest.raises(KeyError):
        await cache.refresh_one(cfg, "nonexistent", timeout_s=2.0)


async def test_cache_all_results_returns_copy(tmp_path: Path) -> None:
    """all_results returns a copy so callers can't mutate the
    cache through the return value."""
    cfg = _cfg(tmp_path, [_ollama_model()])
    body = _ollama_show_response(parameters="num_ctx 4096")
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        cache = ContextWindowCache()
        await cache.populate(cfg, timeout_s=2.0)

    snapshot = cache.all_results()
    snapshot[("ollama", "fake")] = ContextWindowResult(
        tokens=999,
        source="unknown",
        detail="not real",
        discovered_at=0.0,
    )
    # Original cache unchanged.
    assert cache.get("ollama", "fake") is None

"""Phase 9b task 7 — alias-bound embedder + app.state wiring.

Covers the config → embedder → provider path: the AliasEmbedder maps a
model to the right LiteLLM embedding call and caches the dimension from
the first response, and create_app wires a LocalRetrievalProvider onto
app.state exactly when memory.embedding_alias is bound (off otherwise).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.app import create_app
from gateway.config import ModelConfig
from gateway.retrieval import AliasEmbedder, LocalRetrievalProvider
from gateway.retrieval import embedder as embedder_mod
from gateway.retrieval.base import RetrievalError

from ._fixtures import build_test_config


async def test_alias_embedder_calls_litellm_and_caches_dim(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_aembedding(*, input: list[str], **kwargs: Any) -> dict[str, Any]:
        captured["input"] = input
        captured["kwargs"] = kwargs
        return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in input]}

    monkeypatch.setattr(embedder_mod.litellm, "aembedding", _fake_aembedding)

    model = ModelConfig(
        id="embed1", backend="ollama", model="nomic-embed-text", endpoint="http://localhost:11434"
    )
    emb = AliasEmbedder(model)
    assert emb.dim == 0  # unknown until first embed
    assert emb.model_id == "embed1"

    vecs = await emb.embed(["alpha", "beta"])
    assert len(vecs) == 2
    assert vecs[0] == [0.1, 0.2, 0.3]
    assert emb.dim == 3  # cached from the response
    # Ollama embeddings use the ``ollama/`` prefix (not ``ollama_chat/``).
    assert captured["kwargs"]["model"] == "ollama/nomic-embed-text"
    assert captured["kwargs"]["api_base"] == "http://localhost:11434"


async def test_alias_embedder_openai_passes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_aembedding(*, input: list[str], **kwargs: Any) -> dict[str, Any]:
        captured["kwargs"] = kwargs
        return {"data": [{"embedding": [1.0]} for _ in input]}

    monkeypatch.setattr(embedder_mod.litellm, "aembedding", _fake_aembedding)
    model = ModelConfig(
        id="e", backend="openai", model="text-embed", endpoint="https://x.example/v1"
    )
    await AliasEmbedder(model, "sk-test").embed(["hi"])
    assert captured["kwargs"]["model"] == "openai/text-embed"
    assert captured["kwargs"]["api_key"] == "sk-test"


def test_anthropic_backend_has_no_embeddings() -> None:
    model = ModelConfig(id="a", backend="anthropic", model="claude-x")
    with pytest.raises(RetrievalError):
        embedder_mod._embedding_kwargs(model, "sk")


def test_create_app_wires_provider_when_embedding_alias_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    cfg.memory.embedding_alias = "fitt-default"  # resolves to qwen-big (ollama)

    app = create_app(cfg)
    assert isinstance(app.state.retrieval_provider, LocalRetrievalProvider)


def test_create_app_no_provider_without_embedding_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    # embedding_alias unset by default -> retrieval off.

    app = create_app(cfg)
    assert app.state.retrieval_provider is None


def test_create_app_bad_embedding_alias_degrades_to_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    cfg.memory.embedding_alias = "no-such-alias"

    # A bad alias must not crash boot — retrieval degrades to off.
    app = create_app(cfg)
    assert app.state.retrieval_provider is None

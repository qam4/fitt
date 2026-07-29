"""Alias-bound embedder for the local retrieval provider (Phase 9b task 7).

Turns a FITT model *alias* (resolved to a :class:`ModelConfig`) into an
:class:`~gateway.retrieval.local.Embedder` the local provider can call.
Embeddings go through LiteLLM's embedding endpoint, mirroring how the
router dispatches chat — so "the embedding model is configuration, not
architecture" (Principle 7): swap ``memory.embedding_alias`` in config,
rebuild the index, no code change.

The vector dimension is discovered from the first embedding response and
cached (``dim`` is 0 until then). The provider only reads ``dim`` after
it has embedded at least once on the write/query path, so the cached
value is populated when it matters; :meth:`LocalRetrievalProvider.status`
tolerates a 0 (unknown) dim and simply reports the stored index dim.
"""

from __future__ import annotations

from typing import Any

import litellm

from ..config import ModelConfig
from .base import RetrievalError


def _embedding_kwargs(model: ModelConfig, key: str | None) -> dict[str, Any]:
    """LiteLLM kwargs for an embedding call. Mirrors the router's chat
    mapping but with embedding-provider prefixes (Ollama embeddings use
    the ``ollama/`` prefix, not ``ollama_chat/``)."""
    kwargs: dict[str, Any] = {}
    match model.backend:
        case "ollama":
            kwargs["model"] = f"ollama/{model.model}"
            assert model.endpoint
            kwargs["api_base"] = model.endpoint
        case "openai":
            kwargs["model"] = f"openai/{model.model}"
            assert model.endpoint
            kwargs["api_base"] = model.endpoint
            if key:
                kwargs["api_key"] = key
        case "openrouter":
            kwargs["model"] = f"openrouter/{model.model}"
            if key:
                kwargs["api_key"] = key
        case "anthropic":
            raise RetrievalError(
                "anthropic has no embedding endpoint; bind memory.embedding_alias "
                "to an ollama or openai-compatible embedding model"
            )
    return kwargs


class AliasEmbedder:
    """Embedder backed by a FITT model alias, via LiteLLM.

    Satisfies the :class:`~gateway.retrieval.local.Embedder` protocol.
    Built at boot from the resolved primary :class:`ModelConfig` for
    ``memory.embedding_alias`` plus its API key (if any)."""

    def __init__(self, model: ModelConfig, api_key: str | None = None) -> None:
        self._model = model
        self._api_key = api_key
        self._dim = 0

    @property
    def model_id(self) -> str:
        return self._model.id

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        kwargs = _embedding_kwargs(self._model, self._api_key)
        resp = await litellm.aembedding(input=texts, **kwargs)
        vecs = [list(item["embedding"]) for item in resp["data"]]
        if vecs:
            self._dim = len(vecs[0])
        return vecs

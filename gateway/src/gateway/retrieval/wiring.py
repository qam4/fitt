"""Build the retrieval provider from config (Phase 9).

Shared by ``create_app`` (boot wiring) and the ``fitt memory`` CLI so
the "alias -> embedder -> LocalRetrievalProvider" assembly lives in one
place. Returns ``None`` when retrieval isn't configured
(``memory.embedding_alias`` unset); raises on a bad alias so the caller
decides whether to degrade (app: warn + disable) or fail (CLI: print +
exit)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import fitt_home
from .embedder import AliasEmbedder
from .local import LocalRetrievalProvider

if TYPE_CHECKING:
    from ..config import Config


def build_retrieval_provider(config: Config) -> LocalRetrievalProvider | None:
    """Return a configured :class:`LocalRetrievalProvider`, or ``None``
    when ``memory.embedding_alias`` is unset (retrieval off)."""
    alias = getattr(config.memory, "embedding_alias", None)
    if not alias:
        return None
    model = config.resolve_alias(alias)[0]
    key = None
    if config.secrets is not None:
        key = config.secrets.api_key_for(model.backend, model_id=model.id)
    return LocalRetrievalProvider(fitt_home() / "memory" / "index.db", AliasEmbedder(model, key))

"""Build the retrieval provider from config (Phase 9).

Shared by ``create_app`` (boot wiring) and the ``fitt memory`` CLI so
the "alias -> embedder -> LocalRetrievalProvider" assembly lives in one
place. Returns ``None`` when retrieval isn't configured
(``memory.embedding_alias`` unset); raises on a bad alias so the caller
decides whether to degrade (app: warn + disable) or fail (CLI: print +
exit)."""

from __future__ import annotations

from pathlib import Path
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
    return LocalRetrievalProvider(index_path(config), AliasEmbedder(model, key))


def index_path(config: Config) -> Path:
    """Where the retrieval index lives.

    ``memory.index_path`` when set, else ``$FITT_HOME/memory/index.db``.
    Callers that relocate the other memory paths (the e2e harness's
    isolated run home) set it so eval turns don't land in the
    operator's real index."""
    configured = getattr(config.memory, "index_path", None)
    if configured is not None:
        return Path(configured)
    return fitt_home() / "memory" / "index.db"

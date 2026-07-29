"""Cross-session retrieval layer (Phase 9 — Memory v1).

Public surface is the provider contract + its value types; concrete
providers (``honcho`` / ``local``) register in Phase 9b. See
``.kiro/specs/phase9-memory-v1/`` for the spec.
"""

from __future__ import annotations

from .base import (
    IndexStats,
    IndexStatus,
    MemoryDoc,
    RetrievalError,
    RetrievalHit,
    RetrievalMode,
    RetrievalProvider,
    RetrievalQuery,
    RetrievalScope,
)
from .embedder import AliasEmbedder
from .local import Embedder, LocalRetrievalProvider

__all__ = [
    "AliasEmbedder",
    "Embedder",
    "IndexStats",
    "IndexStatus",
    "LocalRetrievalProvider",
    "MemoryDoc",
    "RetrievalError",
    "RetrievalHit",
    "RetrievalMode",
    "RetrievalProvider",
    "RetrievalQuery",
    "RetrievalScope",
]

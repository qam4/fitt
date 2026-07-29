"""Phase 9e — automatic prefetch of recalled context.

When ``memory.prefetch_enabled`` is on, the chat handler asks the
retrieval provider for the most relevant prior excerpts for the current
user message and injects them into a bounded, provenance-labeled
``[Recalled context]`` system block — distinct from ``[Learned
corrections]`` and the recency history, so the model reads recalled
memory as history, not current-turn fact (Property 5).

Opt-in by design: unlike indexing, prefetch is a retrieval call *on* the
request path and costs token budget. Best-effort — any failure yields an
empty block so a flaky/absent backend never breaks the turn.
"""

from __future__ import annotations

import logging

from .base import RetrievalProvider, RetrievalQuery

_log = logging.getLogger(__name__)

_EXCERPT_CAP = 240
_BLOCK_HEADER = "[Recalled context]"


async def prefetch_block(
    provider: RetrievalProvider,
    *,
    query: str,
    session_id: str,
    k: int = 3,
) -> str:
    """Return a ``[Recalled context]`` block for ``query`` (current
    session scope), or an empty string when there's nothing to add or
    retrieval fails. Never raises."""
    if not query.strip():
        return ""
    try:
        hits = await provider.search(
            RetrievalQuery(text=query, mode="semantic", scope="session", session_id=session_id, k=k)
        )
    except Exception as exc:
        _log.warning("memory.prefetch.failed", extra={"error": str(exc)})
        return ""
    if not hits:
        return ""
    lines = [
        _BLOCK_HEADER,
        "",
        "Possibly-relevant excerpts recalled from earlier conversations. "
        "Treat as memory of what was said, not as current-turn fact:",
    ]
    for h in hits:
        excerpt = h.excerpt.strip().replace("\n", " ")
        if len(excerpt) > _EXCERPT_CAP:
            excerpt = excerpt[: _EXCERPT_CAP - 3] + "..."
        lines.append(f"- [{h.session_id} {h.date.isoformat()}] {excerpt}")
    return "\n".join(lines)

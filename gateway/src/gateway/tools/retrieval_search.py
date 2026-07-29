"""Phase 9d — the ``memory_search`` tool (cross-session recall).

One tool, three shapes + two modes, following the Hermes
discovery/scroll/browse pattern rather than proliferating tools:

* **discovery**: pass ``query`` -> ranked excerpts (semantic or keyword).
* **scroll**: pass ``anchor`` -> the turns neighboring that anchor.
* **browse**: pass neither -> the most recent turns.

Read-only, so the default bucket is ``auto``. The provider is looked up
off :class:`ToolContext` (``ctx.retrieval``); the tool is only registered
when retrieval is configured, so a live ``None`` only happens in tests
(where it returns a readable error rather than crashing).
"""

from __future__ import annotations

import logging
from typing import Any

from ..retrieval.base import RetrievalQuery
from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

_log = logging.getLogger(__name__)

_MAX_LIMIT = 20

_SCHEMA_MEMORY_SEARCH: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "What to recall, in natural language ('the training-run "
                "flakiness we discussed') or an exact phrase/identifier "
                "('pid 456'). Omit to browse the most recent turns."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["semantic", "keyword"],
            "default": "semantic",
            "description": (
                "'semantic' matches on meaning (the gist); 'keyword' "
                "matches exact tokens/phrases. Use keyword for exact "
                "identifiers or error strings."
            ),
        },
        "scope": {
            "type": "string",
            "enum": ["session", "all"],
            "default": "session",
            "description": (
                "'session' (default) searches only the current "
                "conversation; 'all' searches across every session — use "
                "it for 'what did I say about X in any project'."
            ),
        },
        "limit": {
            "type": "integer",
            "default": 5,
            "description": "Max results to return (1-20).",
        },
        "anchor": {
            "type": "string",
            "description": (
                "Optional: a turn anchor from a previous result to scroll "
                "around (returns the neighboring turns in that session)."
            ),
        },
    },
    "required": [],
    "additionalProperties": False,
}


async def _tool_memory_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    provider = getattr(ctx, "retrieval", None)
    if provider is None:
        return ToolResult.error(
            "cross-session memory retrieval isn't configured on this "
            "gateway (set memory.embedding_alias in config.yaml)"
        )

    query = args.get("query") or ""
    mode = args.get("mode") or "semantic"
    scope = args.get("scope") or "session"
    anchor = args.get("anchor") or None
    if mode not in ("semantic", "keyword"):
        return ToolResult.error("'mode' must be 'semantic' or 'keyword'")
    if scope not in ("session", "all"):
        return ToolResult.error("'scope' must be 'session' or 'all'")
    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        return ToolResult.error("'limit' must be an integer")
    limit = max(1, min(limit, _MAX_LIMIT))

    q = RetrievalQuery(
        text=query,
        mode=mode,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        session_id=ctx.session_key,
        k=limit,
        anchor=anchor,
    )
    try:
        hits = await provider.search(q)
    except Exception as exc:
        return ToolResult.error(f"memory search failed: {exc}")

    if not hits:
        return ToolResult.ok("no matching memory found")
    lines = [f"[{h.session_id} {h.date.isoformat()}] {h.excerpt}" for h in hits]
    return ToolResult.ok("\n\n".join(lines))


def build_retrieval_tools() -> list[Tool]:
    """Return the ``memory_search`` tool. Registered only when retrieval
    is configured (see build_core_tool_registry)."""
    return [
        Tool(
            name="memory_search",
            description=(
                "Search past conversations for relevant context. Call this "
                "when the user refers to something from before ('remember "
                "when we discussed X', 'what did I say about Y') that isn't "
                "in the current context. Returns excerpts labeled with their "
                "session and date. Read-only. Default scope is the current "
                "session; set scope='all' for cross-session recall."
            ),
            schema=_SCHEMA_MEMORY_SEARCH,
            callable=_tool_memory_search,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
        ),
    ]

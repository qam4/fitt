"""``web_search`` dispatcher tool (Phase 4.11).

A single tool name (``web_search``) backed by a pluggable
provider layer. The agent calls
``web_search(query, limit=5)`` and gets back
``{success, data: {web: [{title, url, snippet, position}]}}``
regardless of which backend is configured.

The tool description embeds the active backend name (cf.
:func:`build_web_search_tool`) so the operator inspecting the
system prompt with ``log_bodies: true`` immediately sees what
serves search, and the model gets a small signal about which
backend is in play.

Failure isolation is total: any exception raised by the
provider's ``search()`` call gets caught and surfaced as a
structured ``{"success": False, "error": ...}`` envelope.
Stack traces never reach the agent or the chat handler.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult
from .web_providers import WEB_SEARCH_REGISTRY

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- bounds

_QUERY_MAX_CODEPOINTS = 500
"""Hard cap on query length. DuckDuckGo's UI caps at ~500 chars
in practice; longer queries return weird results or 414. Reject
loud rather than silently truncate."""

_LIMIT_MIN = 1
_LIMIT_MAX = 20
_LIMIT_DEFAULT = 5
"""Result-count bounds. Below 1 makes no sense; above 20 starts
to bloat the agent's context with marginal-relevance hits.
Default 5 matches typical search-tool conventions."""

_ERROR_MESSAGE_MAX_CODEPOINTS = 240
"""Truncation cap on error messages surfaced in the response.
Mirrors the DDGS provider's own cap so a deeply-nested error
doesn't escape via the dispatcher."""


# --------------------------------------------------------------- schema

_SCHEMA_WEB_SEARCH: dict[str, Any] = {
    "name": "web_search",
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Search query. Plain text; URL encoding handled by the provider. 1-500 characters."
            ),
        },
        "limit": {
            "type": "integer",
            "description": ("Number of results to return. Default 5; clamped to [1, 20]."),
            "default": _LIMIT_DEFAULT,
            "minimum": _LIMIT_MIN,
            "maximum": _LIMIT_MAX,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- helpers


def _validate_query(args: dict[str, Any]) -> str | ToolResult:
    """Pull and validate the ``query`` argument.

    Returns the validated string on success, or a
    :class:`ToolResult` error on failure.
    """
    raw = args.get("query")
    if raw is None or raw == "":
        return ToolResult.error("Missing required argument: query")
    if not isinstance(raw, str):
        return ToolResult.error(f"Argument 'query' must be a string (got {type(raw).__name__})")
    stripped = raw.strip()
    if not stripped:
        return ToolResult.error("Argument 'query' is empty after stripping whitespace")
    if len(stripped) > _QUERY_MAX_CODEPOINTS:
        return ToolResult.error(
            f"Argument 'query' exceeds {_QUERY_MAX_CODEPOINTS} codepoints (got {len(stripped)})"
        )
    return stripped


def _validate_limit(args: dict[str, Any]) -> int | ToolResult:
    """Pull, validate, and clamp the ``limit`` argument.

    Missing limit defaults to ``_LIMIT_DEFAULT``. Non-integer
    values return an error. Out-of-range values get clamped
    silently to ``[_LIMIT_MIN, _LIMIT_MAX]`` per Requirement
    1.2's "clamped to the inclusive range" — we don't fail the
    call for a too-big or too-small ``limit`` because the
    request shape is otherwise valid.
    """
    if "limit" not in args or args["limit"] is None:
        return _LIMIT_DEFAULT
    raw = args["limit"]
    # Bools are ints in Python; reject explicitly so
    # ``limit=True`` doesn't silently become ``1``.
    if not isinstance(raw, int) or isinstance(raw, bool):
        return ToolResult.error(f"Argument 'limit' must be an integer (got {type(raw).__name__})")
    return max(_LIMIT_MIN, min(_LIMIT_MAX, raw))


def _truncate_error(message: str) -> str:
    if len(message) <= _ERROR_MESSAGE_MAX_CODEPOINTS:
        return message
    return message[:_ERROR_MESSAGE_MAX_CODEPOINTS] + "..."


# --------------------------------------------------------------- dispatcher


async def _tool_web_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Dispatch a web search via the configured provider.

    Validates ``query`` and ``limit``, looks up the active
    provider in :data:`WEB_SEARCH_REGISTRY`, dispatches,
    catches every exception, logs structured telemetry, and
    returns a :class:`ToolResult` whose payload is JSON-encoded
    success/error envelope.
    """
    backend = ctx.web_search_backend or "ddgs"

    query_or_error = _validate_query(args)
    if isinstance(query_or_error, ToolResult):
        return query_or_error
    query = query_or_error

    limit_or_error = _validate_limit(args)
    if isinstance(limit_or_error, ToolResult):
        return limit_or_error
    limit = limit_or_error

    provider = WEB_SEARCH_REGISTRY.get(backend)
    if provider is None:
        available = sorted(WEB_SEARCH_REGISTRY.keys()) or ["(none)"]
        return ToolResult.error(
            f"Configured web search backend '{backend}' is not registered. "
            f"Available: {', '.join(available)}."
        )

    started_at = time.perf_counter()
    try:
        result = provider.search(query, limit)
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        _log.warning(
            "web_search.failed",
            extra={
                "event": "web_search.failed",
                "backend": backend,
                "query_chars": len(query),
                "latency_ms": latency_ms,
                "error": _truncate_error(f"{type(exc).__name__}: {exc}"),
            },
        )
        return ToolResult.error(f"web_search dispatch error: {_truncate_error(str(exc))}")

    latency_ms = int((time.perf_counter() - started_at) * 1000)

    if not isinstance(result, dict) or "success" not in result:
        # Provider returned something out of contract. Surface
        # as a failure but log it as a provider bug, not a
        # generic search failure.
        _log.warning(
            "web_search.provider_returned_invalid_envelope",
            extra={
                "event": "web_search.provider_returned_invalid_envelope",
                "backend": backend,
                "got_type": type(result).__name__,
            },
        )
        return ToolResult.error(
            f"web search provider '{backend}' returned an invalid response envelope"
        )

    if result.get("success"):
        web_results = (result.get("data") or {}).get("web") or []
        result_count = len(web_results) if isinstance(web_results, list) else 0
        _log.info(
            "web_search.completed",
            extra={
                "event": "web_search.completed",
                "backend": backend,
                "query_chars": len(query),
                "result_count": result_count,
                "latency_ms": latency_ms,
            },
        )
        return ToolResult.ok(json.dumps(result, ensure_ascii=False))

    error_message = str(result.get("error") or "unknown error")
    _log.warning(
        "web_search.failed",
        extra={
            "event": "web_search.failed",
            "backend": backend,
            "query_chars": len(query),
            "latency_ms": latency_ms,
            "error": _truncate_error(error_message),
        },
    )
    return ToolResult.error(_truncate_error(error_message))


# --------------------------------------------------------------- factory


def build_web_search_tool(backend_name: str) -> Tool:
    """Construct the ``web_search`` :class:`Tool` for the given
    active backend.

    The tool description embeds the active backend name so the
    operator can see at a glance what's serving search. Per
    Requirement 5, the description is computed once at
    gateway-boot time and stays stable across the process
    lifetime (matching prompt-cache stability).
    """
    description = (
        f"Search the web for fresh info "
        f"(active backend: {backend_name}). Use for current "
        f"events, recent versions, prices, news. Returns "
        f"structured results with title/url/snippet."
    )

    return Tool(
        name="web_search",
        description=description,
        schema=_SCHEMA_WEB_SEARCH,
        callable=_tool_web_search,
        default_bucket=ApprovalBucket.AUTO,
        requires_project=False,
        kind="inline",
    )


def build_web_search_tools(backend_name: str) -> list[Tool]:
    """Return the Phase-4.11 web search tools.

    Wraps :func:`build_web_search_tool` in a list to match the
    ``build_*_tools`` convention every other tool group uses.
    Today returns a single-element list; if/when ``web_extract``
    or ``web_crawl`` ship, they slot in here.

    Triggers provider discovery as a side effect so that
    :data:`WEB_SEARCH_REGISTRY` is populated before the
    dispatcher's first call.
    """
    from .web_providers import discover_providers

    discover_providers()
    return [build_web_search_tool(backend_name)]

"""DuckDuckGo search provider via the ``ddgs`` PyPI package.

This is FITT's default web search backend — no API key, no
self-hosted infrastructure. The ``ddgs`` package is a thin
wrapper around DuckDuckGo's HTML-search interface; it handles
cooldown, region/safesearch params, and other DDG quirks we'd
otherwise re-discover ourselves.

Why ``ddgs`` instead of hand-rolled HTML scraping (cf.
OpenClaw): trading "one more dependency" for "we don't own the
parser when DDG changes their HTML." Same trade Hermes made.

Failure modes handled:

* Package not importable at boot (operator hasn't installed
  ``ddgs``): :meth:`is_available` returns False.
* Package not importable at call time (something weird, e.g.
  the env mutated): :meth:`search` returns the structured
  install-instruction error.
* DDG rate-limited us / network error: any exception inside
  the ``ddgs.DDGS().text()`` call gets caught and surfaces as
  a structured failure envelope with the truncated message.
"""

from __future__ import annotations

import logging
from typing import Any

from . import WebSearchProvider, register_provider

_log = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE_CHARS = 240
"""Truncation cap for exception messages surfaced in the
``error`` field. Keeps a misbehaving provider from blowing the
agent's context with a 4KB stack trace echo."""


class DDGSWebSearchProvider(WebSearchProvider):
    """DuckDuckGo HTML-scrape search via the ``ddgs`` package.

    No API key needed. Rate limits are enforced server-side by
    DuckDuckGo; the provider surfaces ``DuckDuckGoSearchException``
    and other ddgs errors as ``{"success": False, "error": ...}``
    rather than raising.
    """

    @property
    def name(self) -> str:
        return "ddgs"

    def is_available(self) -> bool:
        """Return True iff the ``ddgs`` package is importable.

        Probes the import once; cheap because Python caches the
        import result. MUST NOT perform network I/O — runs at
        tool-registration time and on every
        ``list_capabilities`` call.
        """
        try:
            import ddgs  # noqa: F401
        except ImportError:
            return False
        return True

    def search(self, query: str, limit: int) -> dict[str, Any]:
        """Execute a DuckDuckGo search and return normalized
        results.

        Returns the success / failure envelope contract from
        :class:`WebSearchProvider.search`. Catches every
        exception per Requirement 3.5 — the dispatcher's
        failure-isolation contract relies on this.
        """
        try:
            from ddgs import DDGS
        except ImportError:
            return {
                "success": False,
                "error": (
                    "ddgs package is not installed — run `uv add ddgs` in the gateway package."
                ),
            }

        # ``DDGS().text`` yields up to ``max_results`` items; we
        # cap defensively in case the package ignores the hint
        # (Hermes does the same).
        safe_limit = max(1, int(limit))

        try:
            web_results: list[dict[str, Any]] = []
            with DDGS() as client:
                for i, hit in enumerate(client.text(query, max_results=safe_limit)):
                    if i >= safe_limit:
                        break
                    url = str(hit.get("href") or hit.get("url") or "")
                    web_results.append(
                        {
                            "title": str(hit.get("title", "")),
                            "url": url,
                            "snippet": str(hit.get("body", "")),
                            "position": i + 1,
                        }
                    )
        except Exception as exc:
            _log.warning(
                "web_search.provider_failed",
                extra={
                    "event": "web_search.provider_failed",
                    "provider_name": self.name,
                    "exception_class": type(exc).__name__,
                    "exception_message": str(exc),
                },
            )
            message = f"DuckDuckGo search failed: {exc}"
            if len(message) > _MAX_ERROR_MESSAGE_CHARS:
                message = message[:_MAX_ERROR_MESSAGE_CHARS] + "..."
            return {"success": False, "error": message}

        return {"success": True, "data": {"web": web_results}}


register_provider(DDGSWebSearchProvider())

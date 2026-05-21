"""Web search provider plugin layer (Phase 4.11).

The single ``web_search`` tool the agent calls dispatches to one
of these providers based on ``config.web.search_backend``. The
agent's tool-call code path stays identical regardless of which
provider serves the call — that's the whole reason for the
abstraction.

Architecture mirrors the convergent OpenClaw / Hermes pattern:
each provider is a class implementing the same ABC, lives in
its own file under this directory, and registers itself at
import time via :func:`register_provider`. The boot-time
:func:`discover_providers` import-walk wires every concrete
provider into :data:`WEB_SEARCH_REGISTRY`; the dispatcher tool
looks up the configured backend there.

v1 ships one provider (``ddgs``, the keyless DuckDuckGo
backend). The ABC declares ``supports_search`` /
``supports_extract`` / ``supports_crawl`` capability flags so
future providers can advertise extract or crawl support without
ABC changes; the registry only consults ``supports_search`` in
this phase.

Adding a provider later (SearXNG, Brave-free, Exa, ...) is a
single new file in this directory plus a one-line operator
config change. No registry refactor.
"""

from __future__ import annotations

import abc
import logging
from typing import Any

_log = logging.getLogger(__name__)


class WebSearchProvider(abc.ABC):
    """Plugin-facing ABC for web search backends.

    Subclasses MUST implement :attr:`name` and :meth:`search`.
    They MAY override :meth:`is_available` to advertise
    availability cheaply (e.g., is the underlying SDK
    importable, are required env vars present); the base
    implementation returns ``True`` so simple providers don't
    have to think about it.

    The capability flags (:meth:`supports_search`,
    :meth:`supports_extract`, :meth:`supports_crawl`) are
    forward-compat for Phase 4.12+ when ``web_extract`` and
    ``web_crawl`` companion tools may ship. Today's registry
    only consults ``supports_search``.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable identifier used in ``web.search_backend`` config keys.

        Lowercase; hyphens permitted (cf. ``brave-free``). One
        word per provider so the operator never types something
        long.
        """

    def is_available(self) -> bool:
        """Return ``True`` when the provider can serve calls.

        Cheap check: is the underlying SDK importable, are
        required env vars present. MUST NOT perform network I/O
        — this runs at tool-registration time and on every
        ``list_capabilities`` call. A network probe per call
        would be a measurable cold-path tax.

        Default returns ``True``; concrete providers override
        when they have a real availability check.
        """
        return True

    def supports_search(self) -> bool:
        """Return ``True`` if this provider implements :meth:`search`.

        Default ``True``: a provider in this directory is
        primarily a search backend.
        """
        return True

    def supports_extract(self) -> bool:
        """Return ``True`` if this provider can also fetch and
        extract URL content.

        Default ``False``. Tavily / Firecrawl etc. would
        override; v1 has no consumer.
        """
        return False

    def supports_crawl(self) -> bool:
        """Return ``True`` if this provider can crawl from a
        seed URL.

        Default ``False``. Niche capability; v1 has no consumer.
        """
        return False

    @abc.abstractmethod
    def search(self, query: str, limit: int) -> dict[str, Any]:
        """Execute a web search.

        Returns ``{"success": True, "data": {"web": [{title,
        url, snippet, position?}, ...]}}`` on success, or
        ``{"success": False, "error": <str>}`` on failure.

        MUST NOT raise. Catch every exception, log at WARNING,
        return the error envelope. Failure isolation is the
        dispatcher's contract; providers help by not
        propagating.
        """


# --------------------------------------------------------------- registry


WEB_SEARCH_REGISTRY: dict[str, WebSearchProvider] = {}
"""Module-level map from provider name → instance. Populated at
boot via :func:`discover_providers`. The dispatcher tool looks
up the configured backend here."""


def register_provider(provider: WebSearchProvider) -> None:
    """Add a provider to :data:`WEB_SEARCH_REGISTRY`.

    Called at import time by each concrete provider module.
    Last-writer-wins on name collisions, but in practice the
    registry is populated once per process and a collision is a
    bug we'd want to know about — so we log a WARNING when one
    happens.
    """
    name = provider.name
    if name in WEB_SEARCH_REGISTRY:
        _log.warning(
            "web_search.provider_name_collision",
            extra={
                "event": "web_search.provider_name_collision",
                "provider_name": name,
                "previous_class": type(WEB_SEARCH_REGISTRY[name]).__name__,
                "new_class": type(provider).__name__,
            },
        )
    WEB_SEARCH_REGISTRY[name] = provider


_discovered = False


def discover_providers() -> None:
    """Import every concrete provider module so each calls
    :func:`register_provider` at import time. Idempotent;
    called once at gateway boot.

    The discovery is "import every ``.py`` file in this
    directory other than ``__init__.py``." That keeps the seam
    for adding a new provider as small as possible: drop a
    file, the import-time side effect handles registration.
    """
    global _discovered
    if _discovered:
        return

    import importlib
    import pkgutil

    package_path = __path__
    for module_info in pkgutil.iter_modules(package_path):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module_info.name}")

    _discovered = True
    _log.info(
        "web_search.providers_discovered",
        extra={
            "event": "web_search.providers_discovered",
            "registered": sorted(WEB_SEARCH_REGISTRY.keys()),
        },
    )

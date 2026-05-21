"""Unit tests for the WebSearchProvider ABC + registry (Phase 4.11)."""

from __future__ import annotations

from typing import Any

from gateway.tools.web_providers import (
    WEB_SEARCH_REGISTRY,
    WebSearchProvider,
    discover_providers,
    register_provider,
)


class _StubProvider(WebSearchProvider):
    """Minimal concrete provider for ABC-shape tests."""

    def __init__(self, name: str = "stub") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, limit: int) -> dict[str, Any]:
        return {"success": True, "data": {"web": []}}


def test_register_provider_indexes_by_name():
    """``register_provider`` keys the registry by ``name``."""
    p = _StubProvider("test-register")
    register_provider(p)
    try:
        assert WEB_SEARCH_REGISTRY["test-register"] is p
    finally:
        WEB_SEARCH_REGISTRY.pop("test-register", None)


def test_discover_providers_imports_ddgs():
    """``discover_providers`` walks the package and imports
    every concrete provider module so each registers itself."""
    discover_providers()
    assert "ddgs" in WEB_SEARCH_REGISTRY
    assert WEB_SEARCH_REGISTRY["ddgs"].name == "ddgs"


def test_provider_abc_default_capability_flags():
    """Default capability flags: search True, extract False,
    crawl False. Concrete providers override as needed."""
    p = _StubProvider()
    assert p.supports_search() is True
    assert p.supports_extract() is False
    assert p.supports_crawl() is False


def test_provider_abc_default_is_available_returns_true():
    """Base ABC's ``is_available`` returns True when not overridden."""
    p = _StubProvider()
    assert p.is_available() is True


def test_register_provider_logs_collision_and_replaces(
    caplog,
):
    """A second registration under the same name logs WARNING
    and replaces the old instance — last-writer-wins."""
    import logging

    caplog.set_level(logging.WARNING, logger="gateway.tools.web_providers")
    a = _StubProvider("test-collide")
    b = _StubProvider("test-collide")
    register_provider(a)
    register_provider(b)
    try:
        assert WEB_SEARCH_REGISTRY["test-collide"] is b
        assert any(
            getattr(r, "event", None) == "web_search.provider_name_collision"
            for r in caplog.records
        )
    finally:
        WEB_SEARCH_REGISTRY.pop("test-collide", None)

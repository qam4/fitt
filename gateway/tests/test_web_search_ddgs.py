"""Unit tests for the DDGS web search provider (Phase 4.11)."""

from __future__ import annotations

import builtins
import logging
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from gateway.tools.web_providers.ddgs import DDGSWebSearchProvider

# --------------------------------------------------------------- name + availability


def test_ddgs_name_is_ddgs():
    p = DDGSWebSearchProvider()
    assert p.name == "ddgs"


def test_ddgs_is_available_when_package_installed():
    """``ddgs`` is in the gateway's deps; should always be importable."""
    p = DDGSWebSearchProvider()
    assert p.is_available() is True


def test_ddgs_is_available_when_package_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Simulate import failure and confirm
    ``is_available`` returns False without raising."""
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ddgs" or name.startswith("ddgs."):
            raise ImportError("simulated missing ddgs")
        return real_import(name, *args, **kwargs)

    # Drop any cached ddgs import so the fake takes effect.
    monkeypatch.delitem(sys.modules, "ddgs", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    p = DDGSWebSearchProvider()
    assert p.is_available() is False


# --------------------------------------------------------------- search


def _patch_ddgs_text(monkeypatch: pytest.MonkeyPatch, hits: list[dict[str, Any]]):
    """Replace ``ddgs.DDGS`` with a stub whose ``text`` yields ``hits``."""
    fake_client = MagicMock()
    fake_client.text.return_value = iter(hits)
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    fake_DDGS = MagicMock(return_value=fake_client)
    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", fake_DDGS)
    return fake_DDGS, fake_client


def test_ddgs_search_returns_normalized_results(
    monkeypatch: pytest.MonkeyPatch,
):
    """Provider normalizes ddgs's ``{title, href, body}`` into
    SearchResult shape ``{title, url, snippet, position}``."""
    hits = [
        {
            "title": "Python 3.13.5",
            "href": "https://python.org/3.13.5",
            "body": "Bugfix release.",
        },
        {
            "title": "Python 3.13",
            "href": "https://python.org/3.13",
            "body": "Major release.",
        },
    ]
    _patch_ddgs_text(monkeypatch, hits)

    p = DDGSWebSearchProvider()
    out = p.search("python latest", limit=5)

    assert out["success"] is True
    web = out["data"]["web"]
    assert len(web) == 2
    assert web[0] == {
        "title": "Python 3.13.5",
        "url": "https://python.org/3.13.5",
        "snippet": "Bugfix release.",
        "position": 1,
    }
    assert web[1]["position"] == 2


def test_ddgs_search_caps_results_at_limit(
    monkeypatch: pytest.MonkeyPatch,
):
    """If ddgs ignores ``max_results`` and returns extra hits,
    the provider caps defensively."""
    hits = [{"title": f"r{i}", "href": f"https://x/{i}", "body": "..."} for i in range(10)]
    _patch_ddgs_text(monkeypatch, hits)

    p = DDGSWebSearchProvider()
    out = p.search("anything", limit=3)

    assert out["success"] is True
    assert len(out["data"]["web"]) == 3


def test_ddgs_search_handles_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """ddgs raising any exception → success=False, structured
    error, WARNING log with provider_failed event."""
    caplog.set_level(logging.WARNING, logger="gateway.tools.web_providers.ddgs")

    fake_client = MagicMock()
    fake_client.text.side_effect = RuntimeError("rate limited")
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", MagicMock(return_value=fake_client))

    p = DDGSWebSearchProvider()
    out = p.search("anything", limit=5)

    assert out["success"] is False
    assert "rate limited" in out["error"]
    assert "DuckDuckGo search failed" in out["error"]

    failed_records = [
        r for r in caplog.records if getattr(r, "event", None) == "web_search.provider_failed"
    ]
    assert len(failed_records) == 1
    assert failed_records[0].provider_name == "ddgs"
    assert failed_records[0].exception_class == "RuntimeError"


def test_ddgs_search_truncates_long_error_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    """Exception messages over 240 chars get truncated with ``...``."""
    huge_msg = "x" * 500
    fake_client = MagicMock()
    fake_client.text.side_effect = RuntimeError(huge_msg)
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", MagicMock(return_value=fake_client))

    p = DDGSWebSearchProvider()
    out = p.search("anything", limit=5)

    assert out["error"].endswith("...")
    # The truncated message has at most 240 + 3 = 243 chars
    # plus the "DuckDuckGo search failed: " prefix.
    assert len(out["error"]) <= 240 + 3


def test_ddgs_search_handles_import_error_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
):
    """If ddgs becomes unimportable between boot and a call,
    ``search`` returns the install-instruction error rather
    than raising."""
    # Force the fresh import inside ``search`` to fail.
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ddgs":
            raise ImportError("simulated missing ddgs")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "ddgs", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    p = DDGSWebSearchProvider()
    out = p.search("anything", limit=5)

    assert out["success"] is False
    assert "ddgs package is not installed" in out["error"]
    assert "uv add ddgs" in out["error"]

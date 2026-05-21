"""End-to-end test for the ``web_search`` tool (Phase 4.11, Requirement 6).

Pins the boot-to-tool-call contract: build a Config with
``web.search_backend: "ddgs"``, construct the gateway via
``create_app(config)``, monkey-patch ``ddgs.DDGS`` with a stub
returning canned results, dispatch through the tool registry,
and assert the response shape.

Two scenarios:

1. Happy path — provider returns three canned hits; the tool
   returns the JSON-encoded success envelope with the right
   title/url/snippet/position fields.
2. Failure path — provider raises ``RuntimeError("rate limited")``;
   the tool returns a structured failure with the message
   surfaced and a ``web_search.failed`` log line emitted.

The test mocks only the upstream HTTP call inside ``ddgs.DDGS``
— the rest of the chain (provider, dispatcher, tool registry,
gateway boot) runs end-to-end in-process.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gateway.app import create_app
from gateway.projects import ProjectRegistry
from gateway.tools._types import ToolContext

from ._fixtures import build_test_config


def _ctx(app: Any) -> ToolContext:
    """Build a minimal ToolContext from the live app state.

    Mirrors what ``gateway.chat`` would build during a real
    chat request, minus the audit / events / project_registry
    pieces irrelevant to web_search.
    """
    return ToolContext(
        client="ide",
        session_key="main",
        projects=getattr(app.state, "project_registry", ProjectRegistry()),
        backend=app.state.execution_backend,
        policy=app.state.tool_registry.policy,
        web_search_backend=app.state.config.web.search_backend,
    )


@pytest.fixture
def app_with_ddgs(tmp_path: Path):
    """Construct the gateway app with the default ``ddgs`` backend."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)
    # Stash the config on app.state for the test ctx builder.
    app.state.config = cfg
    yield app


def _patch_ddgs_text(monkeypatch: pytest.MonkeyPatch, hits: list[dict[str, Any]]):
    """Replace ``ddgs.DDGS`` so the provider's ``text`` call yields ``hits``."""
    fake_client = MagicMock()
    fake_client.text.return_value = iter(hits)
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    fake_DDGS = MagicMock(return_value=fake_client)
    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", fake_DDGS)
    return fake_DDGS


# Phase 4.11, Requirement 6
async def test_e2e_web_search_success(app_with_ddgs: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool dispatches through the DDGS provider and returns
    a structured success envelope with three normalized hits."""
    canned = [
        {
            "title": f"Result {i}",
            "href": f"https://example.com/{i}",
            "body": f"Snippet {i}",
        }
        for i in range(1, 4)
    ]
    _patch_ddgs_text(monkeypatch, canned)

    tool = app_with_ddgs.state.tool_registry.lookup("web_search")
    result = await tool.callable(
        {"query": "what's the latest", "limit": 5},
        _ctx(app_with_ddgs),
    )

    assert not result.is_error, result.payload
    parsed = json.loads(result.payload)
    assert parsed["success"] is True
    web = parsed["data"]["web"]
    assert len(web) == 3
    for i, hit in enumerate(web, start=1):
        assert hit["title"] == f"Result {i}"
        assert hit["url"] == f"https://example.com/{i}"
        assert hit["snippet"] == f"Snippet {i}"
        assert hit["position"] == i


# Phase 4.11, Requirement 6
async def test_e2e_web_search_failure(
    app_with_ddgs: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When ``ddgs.DDGS`` raises, the tool returns a structured
    failure with the message surfaced; the gateway logs one
    ``web_search.provider_failed`` WARNING (from the provider)
    AND one ``web_search.failed`` WARNING (from the dispatcher)."""
    caplog.set_level(logging.WARNING)

    fake_client = MagicMock()
    fake_client.text.side_effect = RuntimeError("rate limited")
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", MagicMock(return_value=fake_client))

    tool = app_with_ddgs.state.tool_registry.lookup("web_search")
    result = await tool.callable(
        {"query": "anything"},
        _ctx(app_with_ddgs),
    )

    assert result.is_error
    assert "rate limited" in result.payload

    provider_failed = [
        r for r in caplog.records if getattr(r, "event", None) == "web_search.provider_failed"
    ]
    dispatcher_failed = [
        r for r in caplog.records if getattr(r, "event", None) == "web_search.failed"
    ]
    assert len(provider_failed) == 1
    assert len(dispatcher_failed) == 1
    assert provider_failed[0].provider_name == "ddgs"
    assert dispatcher_failed[0].backend == "ddgs"

"""Unit tests for the ``web_search`` dispatcher tool (Phase 4.11)."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from gateway.projects import ProjectRegistry
from gateway.tools._types import ToolContext
from gateway.tools.web_providers import WEB_SEARCH_REGISTRY, WebSearchProvider
from gateway.tools.web_search import _tool_web_search, build_web_search_tool

# --------------------------------------------------------------- stubs


class _StubProvider(WebSearchProvider):
    """Stub provider whose behaviour is controlled by the test."""

    def __init__(
        self,
        name: str = "stub-provider",
        response: dict[str, Any] | None = None,
        raises: type[Exception] | None = None,
    ) -> None:
        self._name = name
        self._response = response or {"success": True, "data": {"web": []}}
        self._raises = raises
        self.calls: list[tuple[str, int]] = []

    @property
    def name(self) -> str:
        return self._name

    def search(self, query: str, limit: int) -> dict[str, Any]:
        self.calls.append((query, limit))
        if self._raises is not None:
            raise self._raises("stub failure")
        return self._response


@pytest.fixture
def stub_registry():
    """Register a stub provider for each test, clean up after."""
    p = _StubProvider()
    WEB_SEARCH_REGISTRY[p.name] = p
    yield p
    WEB_SEARCH_REGISTRY.pop(p.name, None)


def _ctx(backend: str | None = "stub-provider") -> ToolContext:
    """Build a minimal ToolContext for the dispatcher."""
    return ToolContext(
        client="ide",
        session_key="main",
        projects=ProjectRegistry(),
        web_search_backend=backend,
    )


# --------------------------------------------------------------- argument validation


async def test_dispatcher_rejects_missing_query(stub_registry):
    result = await _tool_web_search({}, _ctx())
    assert result.is_error
    assert "query" in result.payload.lower()


async def test_dispatcher_rejects_empty_query(stub_registry):
    result = await _tool_web_search({"query": ""}, _ctx())
    assert result.is_error
    assert "query" in result.payload.lower()


async def test_dispatcher_rejects_whitespace_query(stub_registry):
    result = await _tool_web_search({"query": "   "}, _ctx())
    assert result.is_error
    assert "empty" in result.payload.lower()


async def test_dispatcher_rejects_query_over_500_codepoints(stub_registry):
    result = await _tool_web_search({"query": "x" * 501}, _ctx())
    assert result.is_error
    assert "500" in result.payload


async def test_dispatcher_rejects_limit_not_int(stub_registry):
    result = await _tool_web_search({"query": "anything", "limit": "five"}, _ctx())
    assert result.is_error
    assert "limit" in result.payload.lower()


async def test_dispatcher_rejects_limit_bool(stub_registry):
    """Bools are ints in Python; reject explicitly."""
    result = await _tool_web_search({"query": "anything", "limit": True}, _ctx())
    assert result.is_error
    assert "limit" in result.payload.lower()


async def test_dispatcher_clamps_limit_below_1(stub_registry):
    """``limit=0`` clamps to 1; provider sees safe value."""
    result = await _tool_web_search({"query": "anything", "limit": 0}, _ctx())
    assert not result.is_error
    assert stub_registry.calls[-1][1] == 1


async def test_dispatcher_clamps_limit_above_20(stub_registry):
    """``limit=100`` clamps to 20."""
    result = await _tool_web_search({"query": "anything", "limit": 100}, _ctx())
    assert not result.is_error
    assert stub_registry.calls[-1][1] == 20


async def test_dispatcher_default_limit_is_5(stub_registry):
    """When ``limit`` is absent, default is 5."""
    result = await _tool_web_search({"query": "anything"}, _ctx())
    assert not result.is_error
    assert stub_registry.calls[-1][1] == 5


# --------------------------------------------------------------- backend dispatch


async def test_dispatcher_unknown_backend_returns_error_with_available_list(
    stub_registry,
):
    result = await _tool_web_search(
        {"query": "anything"},
        _ctx(backend="not-a-thing"),
    )
    assert result.is_error
    assert "not-a-thing" in result.payload
    assert "Available:" in result.payload
    # The stub provider's name appears in "available" since
    # the fixture registered it.
    assert "stub-provider" in result.payload


# --------------------------------------------------------------- success / failure paths


async def test_dispatcher_successful_call_returns_json_envelope(stub_registry):
    """Success path: payload is JSON-encoded provider response."""
    stub_registry._response = {
        "success": True,
        "data": {"web": [{"title": "t", "url": "https://x", "snippet": "s"}]},
    }

    result = await _tool_web_search({"query": "anything"}, _ctx())
    assert not result.is_error

    parsed = json.loads(result.payload)
    assert parsed["success"] is True
    assert parsed["data"]["web"][0]["title"] == "t"


async def test_dispatcher_provider_returning_failure_envelope(stub_registry):
    """Provider returns success=False; dispatcher surfaces error
    text via ToolResult.error."""
    stub_registry._response = {
        "success": False,
        "error": "rate limited by upstream",
    }

    result = await _tool_web_search({"query": "anything"}, _ctx())
    assert result.is_error
    assert "rate limited" in result.payload


async def test_dispatcher_catches_provider_exceptions(stub_registry):
    """Provider raises; dispatcher catches, returns ToolResult.error."""
    stub_registry._raises = RuntimeError

    result = await _tool_web_search({"query": "anything"}, _ctx())
    assert result.is_error
    assert "stub failure" in result.payload


async def test_dispatcher_provider_invalid_envelope(stub_registry):
    """Provider returns something not matching the contract;
    dispatcher surfaces an "invalid envelope" error."""
    stub_registry._response = {"oops": "wrong shape"}

    result = await _tool_web_search({"query": "anything"}, _ctx())
    assert result.is_error
    assert "invalid response envelope" in result.payload.lower()


# --------------------------------------------------------------- logging


async def test_dispatcher_logs_completed_event_on_success(
    stub_registry, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.INFO, logger="gateway.tools.web_search")
    stub_registry._response = {
        "success": True,
        "data": {"web": [{"title": "t", "url": "u", "snippet": "s"}]},
    }

    await _tool_web_search({"query": "kittens"}, _ctx())

    completed = [r for r in caplog.records if getattr(r, "event", None) == "web_search.completed"]
    assert len(completed) == 1
    assert completed[0].backend == "stub-provider"
    assert completed[0].result_count == 1
    assert isinstance(completed[0].latency_ms, int)


async def test_dispatcher_logs_failed_event_on_failure(
    stub_registry, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING, logger="gateway.tools.web_search")
    stub_registry._response = {"success": False, "error": "boom"}

    await _tool_web_search({"query": "kittens"}, _ctx())

    failed = [r for r in caplog.records if getattr(r, "event", None) == "web_search.failed"]
    assert len(failed) == 1
    assert "boom" in failed[0].error


async def test_dispatcher_log_lines_do_not_contain_query(
    stub_registry, caplog: pytest.LogCaptureFixture
):
    """Privacy: log records must NOT contain the raw query text.
    Only ``query_chars`` (length) is logged."""
    caplog.set_level(logging.INFO, logger="gateway.tools.web_search")

    sensitive = "my-very-private-search-string-that-must-not-appear-anywhere"
    await _tool_web_search({"query": sensitive}, _ctx())

    for record in caplog.records:
        # Neither the formatted message nor any extra field
        # should embed the query.
        assert sensitive not in record.getMessage()
        for attr_name in dir(record):
            if attr_name.startswith("_"):
                continue
            value = getattr(record, attr_name, None)
            if isinstance(value, str):
                assert sensitive not in value, attr_name


# --------------------------------------------------------------- factory


def test_build_web_search_tool_embeds_backend_name():
    """The tool description carries the active backend name so
    operators inspecting the system prompt see it without a
    separate status tool."""
    tool = build_web_search_tool("ddgs")
    assert "ddgs" in tool.description
    assert tool.name == "web_search"

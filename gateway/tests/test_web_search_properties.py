"""Property tests for the web_search dispatcher (Phase 4.11, Commit 2).

Two hypothesis-driven invariants:

* **Property 2 — Failure isolation.** For any exception class
  the provider raises, the dispatcher catches it and returns a
  structured failure result without propagating.

* **Property 3 — Limit clamping.** For any integer ``limit``,
  the value forwarded to the provider is in ``[1, 20]``.

These pin the harder-to-example-test invariants from
design.md's Correctness Properties section.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from gateway.projects import ProjectRegistry
from gateway.tools._types import ToolContext
from gateway.tools.web_providers import WEB_SEARCH_REGISTRY, WebSearchProvider
from gateway.tools.web_search import _tool_web_search

_PROVIDER_NAME = "stub-property-provider"


class _RecordingProvider(WebSearchProvider):
    """Stub provider whose behaviour the test controls per call.

    Records the limit it was invoked with so Property 3 can
    assert the dispatcher's clamping behaviour.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.exc: type[Exception] | None = None

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    def search(self, query: str, limit: int) -> dict[str, Any]:
        self.calls.append((query, limit))
        if self.exc is not None:
            raise self.exc("simulated")
        return {"success": True, "data": {"web": []}}


@pytest.fixture
def stub_provider():
    """Register a fresh recording provider per test."""
    p = _RecordingProvider()
    WEB_SEARCH_REGISTRY[p.name] = p
    yield p
    WEB_SEARCH_REGISTRY.pop(p.name, None)


def _ctx() -> ToolContext:
    return ToolContext(
        client="ide",
        session_key="main",
        projects=ProjectRegistry(),
        web_search_backend=_PROVIDER_NAME,
    )


# Phase 4.11, Property 2: Failure isolation
@given(
    exc_class=st.sampled_from(
        [
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            ConnectionError,
            TimeoutError,
            OSError,
            Exception,
        ]
    ),
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_property_dispatcher_catches_any_exception(stub_provider, exc_class):
    """For any exception class the provider raises, the
    dispatcher catches it and returns a structured failure
    without propagating."""
    stub_provider.exc = exc_class

    result = await _tool_web_search({"query": "x"}, _ctx())

    assert result.is_error
    # The dispatcher must NOT raise. If it did, the test
    # would fail with the original exception class instead
    # of getting here.


# Phase 4.11, Property 3: Limit clamping
@given(
    limit=st.integers(min_value=-1000, max_value=1000),
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_property_dispatcher_clamps_limit(stub_provider, limit):
    """For any integer ``limit``, the value forwarded to the
    provider is in ``[1, 20]``."""
    stub_provider.calls.clear()
    stub_provider.exc = None

    result = await _tool_web_search({"query": "x", "limit": limit}, _ctx())

    # Whatever the dispatcher did, the call must have
    # succeeded (the stub returns a success envelope) — so
    # we can read the limit it forwarded.
    assert not result.is_error, result.payload
    assert len(stub_provider.calls) == 1
    forwarded_limit = stub_provider.calls[0][1]
    assert 1 <= forwarded_limit <= 20

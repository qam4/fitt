"""Composability sanity check for the Phase 4.6 e2e fixtures.

Proves P6 from the design: all five fixtures compose without
ordering constraints. One test, four assertions, no lifecycle
logic. If this goes red, every lifecycle test will fail in a
confusing way; keeping it separate lets contributors localise
"is the harness broken" vs "is my test wrong."
"""

from __future__ import annotations

from typing import Any

import httpx

from .._llm_stubs import stub_reply
from .conftest import E2EApprover, E2EClock, StubbedLLM


async def test_fixtures_compose(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_approver: E2EApprover,
    e2e_clock: E2EClock,
    stubbed_llm: StubbedLLM,
) -> None:
    """Request all five fixtures, make one HTTP call, assert wiring.

    Concretely:
    - ``e2e_app.state`` has the phase-4.5 plumbing (approval,
      events, cron_scheduler, cron_runner).
    - ``e2e_client`` can hit ``/health`` without the bearer
      header (exempt prefix).
    - ``e2e_client`` can hit ``/v1/events`` with the bearer and
      get a well-shaped response.
    - ``stubbed_llm`` answers one dispatched chat.
    - ``e2e_clock`` and ``e2e_approver`` instantiate cleanly.
    """
    # Phase 4.5 plumbing is present.
    assert hasattr(e2e_app.state, "approval")
    assert hasattr(e2e_app.state, "events")
    assert hasattr(e2e_app.state, "cron_scheduler")
    assert hasattr(e2e_app.state, "cron_runner")

    # /health is exempt from auth; we don't need the header.
    r = await e2e_client.get("/health")
    assert r.status_code == 200

    # /v1/events requires auth (which we have via the fixture).
    r = await e2e_client.get("/v1/events", params={"limit": 10})
    assert r.status_code == 200, r.text
    assert "events" in r.json()

    # One chat round-trip through the stub.
    stubbed_llm.load([stub_reply("hi there")])
    r = await e2e_client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["content"] == "hi there"
    assert stubbed_llm.remaining() == 0

    # Clock and approver instantiate.
    assert e2e_clock.now > 0
    assert isinstance(e2e_approver, E2EApprover)

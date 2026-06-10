"""U1.4 — detach lifecycle end-to-end.

When a chat turn's approval is slow enough to trip the detach
threshold:
- the HTTP client receives the ``⏳ Approval pending`` placeholder
  with ``X-FITT-Detached: 1`` synchronously,
- the approval later resolves out-of-band (bot tap),
- ``late_tool_result`` lands asynchronously in the event log,
- session memory has both halves of the turn.

``test_detach.py`` covers the same shape at the unit level with
direct approval-middleware calls. This version uses the e2e
fixtures so future tests can copy the pattern via
``e2e_approver`` + ``e2e_clock`` + ``stubbed_llm``. It also
proves the approval goes through the HTTP ``/v1/approvals/*``
surface (and therefore the auth + client-tag path) rather than
an in-process middleware call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import E2EApprover, StubbedLLM, fetch_events, wait_for_event


@pytest.fixture
def detach_threshold() -> float:
    """Enable detach for these tests (the e2e default is off).

    Overrides the conftest ``detach_threshold`` fixture so ``e2e_app``
    wires a 50ms threshold; both tests below deliberately hold the
    approval past it to exercise the detach path.
    """
    return 0.05


async def test_detach_placeholder_then_late_tool_result(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_approver: E2EApprover,
    stubbed_llm: StubbedLLM,
) -> None:
    """POST chat → synchronous placeholder → approve → late event."""
    # Two LLM rounds: the tool call, then the final reply after
    # the tool returns.
    stubbed_llm.load(
        [
            stub_tool_call(
                "write_file",
                {"project": "hub", "path": "a.txt", "content": "x"},
            ),
            stub_reply("wrote a.txt. done."),
        ]
    )

    # Start the chat request in a background task so we can hold
    # the approval long enough to trip the detach threshold
    # (the e2e_app fixture wires it at 50ms) without blocking
    # the test body on the HTTP response.
    async def _post() -> httpx.Response:
        return await e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-smart",
                "messages": [{"role": "user", "content": "write a.txt"}],
                "tool_choice": "auto",
            },
        )

    post_task = asyncio.create_task(_post())

    # Wait for the approval to appear in the middleware's pending
    # map. The detach threshold will trigger shortly after.
    pending = await e2e_approver.wait_for(tool="write_file", timeout_s=3.0)

    # Give the chat handler enough time to detach (threshold =
    # 50ms, approval_timeout = 5s). The handler returns the
    # placeholder once it catches the intermediate timeout.
    r = await post_task
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"].startswith("⏳ Approval pending")
    assert r.headers.get("X-FITT-Detached") == "1"

    # Approve via the HTTP surface (same path the bot uses).
    await e2e_approver.decide(pending["id"], "approve")

    # late_tool_result lands asynchronously.
    evt = await wait_for_event(e2e_client, kind="late_tool_result", timeout_s=3.0)
    assert "wrote a.txt" in evt["body"]
    assert evt["meta"]["tool"] == "write_file"
    assert evt["meta"]["approval_id"] == pending["id"]

    # Memory has the turn persisted. The detached worker writes
    # to the same session as the synchronous path would.
    history_dir = e2e_app.state.config.memory.sessions_dir / "main" / "history"
    assert history_dir.exists(), (
        "detached worker should append the turn to memory; no "
        "history dir for session 'main' means the worker never "
        "wrote the turn"
    )
    entries = list(history_dir.glob("*.md"))
    combined = "\n".join(p.read_text(encoding="utf-8") for p in entries)
    assert "wrote a.txt" in combined

    # Stubs consumed — no runaway dispatch.
    assert stubbed_llm.remaining() == 0


async def test_detach_reject_produces_rejected_event(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    e2e_approver: E2EApprover,
    stubbed_llm: StubbedLLM,
) -> None:
    """Mirror case: reject the detached approval → the
    ``late_tool_rejected`` event lands; the worker backs off
    without executing the tool."""
    stubbed_llm.load(
        [
            stub_tool_call(
                "write_file",
                {"project": "hub", "path": "b.txt", "content": "x"},
            ),
            stub_reply("user rejected; standing down."),
        ]
    )

    async def _post() -> httpx.Response:
        return await e2e_client.post(
            "/v1/chat/completions",
            json={
                "model": "fitt-smart",
                "messages": [{"role": "user", "content": "write b.txt"}],
                "tool_choice": "auto",
            },
        )

    post_task = asyncio.create_task(_post())
    pending = await e2e_approver.wait_for(tool="write_file", timeout_s=3.0)
    r = await post_task
    assert r.headers.get("X-FITT-Detached") == "1"

    await e2e_approver.decide(pending["id"], "reject")

    evt = await wait_for_event(e2e_client, kind="late_tool_rejected", timeout_s=3.0)
    assert "rejected" in evt["body"].lower()

    # No late_tool_result event — the tool didn't run.
    all_events = await fetch_events(e2e_client, limit=1000)
    results = [e for e in all_events if e["kind"] == "late_tool_result"]
    assert results == [], f"a rejected detach must not emit late_tool_result; got {results!r}"

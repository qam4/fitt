"""Phase 5 — lessons end-to-end lifecycle.

Drives the full `learn_add` → approval → persistence → next-
request-injection loop through the HTTP surface. Pins the P1
correctness property: a lesson added in one request shows up
in the system prompt of the next request.
"""

from __future__ import annotations

from typing import Any

import httpx

from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import E2EApprover, StubbedLLM


async def _telegram_approver(
    e2e_client: httpx.AsyncClient,
) -> E2EApprover:
    """Build an approver tagged for telegram (same pattern as
    project_shell e2e test — the ``learn_*`` default bucket
    for `ask` tools is `ask` across telegram/cli/ide)."""
    return E2EApprover(e2e_client, client_tag="telegram")


async def test_learn_add_then_next_request_sees_lesson(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    stubbed_llm: StubbedLLM,
) -> None:
    """First turn: user asks, LLM emits `learn_add`, operator
    approves. Second turn: assert the `[Learned corrections]`
    block in the dispatched system prompt contains the lesson.
    """
    approver = await _telegram_approver(e2e_client)

    # Turn 1: user says "remember X", model calls learn_add,
    # then replies "got it".
    # Turn 2: user asks something unrelated, model replies; we
    # capture the dispatched messages to inspect the system
    # prompt.
    stubbed_llm.load(
        [
            stub_tool_call(
                "learn_add",
                {"text": "always use uv, not pip"},
            ),
            stub_reply("Got it. I'll remember that."),
            stub_reply("Sure, hello."),
        ]
    )

    async with approver.start(lambda p: "approve"):
        r1 = await e2e_client.post(
            "/v1/chat/completions",
            headers={"X-FITT-Client": "telegram"},
            json={
                "model": "fitt-default",
                "messages": [
                    {
                        "role": "user",
                        "content": "remember: always use uv, not pip",
                    }
                ],
                "tool_choice": "auto",
            },
        )
        assert r1.status_code == 200, r1.text

    # Lesson is persisted now — check the store directly.
    lessons = e2e_app.state.lessons.read()
    assert len(lessons) == 1
    assert lessons[0].text == "always use uv, not pip"

    # Turn 2: no tool call this time; just a plain reply.
    r2 = await e2e_client.post(
        "/v1/chat/completions",
        headers={"X-FITT-Client": "telegram"},
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "say hi"}],
            "tool_choice": "auto",
        },
    )
    assert r2.status_code == 200, r2.text

    # The DISPATCH for turn 2 should have the lesson's bullet
    # in the system prompt. The stub captures every dispatch's
    # kwargs. We check for the bullet (``- <text>``) rather than
    # the block header ``[Learned corrections]`` because the
    # ``learn_add`` tool's own description mentions that header
    # — a header-substring check would pass even if the block
    # itself never rendered.
    last_call = stubbed_llm.calls[-1]
    dispatched_messages = last_call.get("messages", [])
    system_msg = next((m for m in dispatched_messages if m.get("role") == "system"), None)
    assert system_msg is not None, (
        "expected a system message in the dispatched turn; "
        "without one the `[Learned corrections]` block can't "
        "be injected"
    )
    assert "- always use uv, not pip" in system_msg["content"], (
        "lesson bullet didn't appear in the system prompt after "
        "learn_add; the learn-add → inject-next-request loop "
        "isn't working"
    )


async def test_learn_remove_hides_lesson_from_next_request(
    e2e_app: Any,
    e2e_client: httpx.AsyncClient,
    stubbed_llm: StubbedLLM,
) -> None:
    """Counter-case: after `learn_remove`, the lesson stops
    appearing in subsequent dispatches."""
    approver = await _telegram_approver(e2e_client)

    # Seed a lesson directly so we don't have to drive
    # learn_add through the stub queue.
    e2e_app.state.lessons.add("always use uv")

    stubbed_llm.load(
        [
            stub_tool_call("learn_remove", {"substring": "uv"}),
            stub_reply("Done, forgot about uv."),
            stub_reply("Sure."),
        ]
    )

    async with approver.start(lambda p: "approve"):
        r1 = await e2e_client.post(
            "/v1/chat/completions",
            headers={"X-FITT-Client": "telegram"},
            json={
                "model": "fitt-default",
                "messages": [{"role": "user", "content": "forget about uv"}],
                "tool_choice": "auto",
            },
        )
        assert r1.status_code == 200, r1.text

    assert e2e_app.state.lessons.read() == []

    # Second turn — no `[Learned corrections]` block since
    # there are no lessons left.
    r2 = await e2e_client.post(
        "/v1/chat/completions",
        headers={"X-FITT-Client": "telegram"},
        json={
            "model": "fitt-default",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "auto",
        },
    )
    assert r2.status_code == 200

    last_call = stubbed_llm.calls[-1]
    dispatched_messages = last_call.get("messages", [])
    system_msg = next((m for m in dispatched_messages if m.get("role") == "system"), None)
    # A system message should still exist (identity is non-empty
    # in the default memory setup), but the actual lesson bullet
    # shouldn't appear. We avoid checking for "[Learned
    # corrections]" verbatim because the `learn_add` tool's own
    # description mentions it — we want to catch the rendered
    # BLOCK, not the documentation of it.
    if system_msg is not None:
        assert "- always use uv" not in system_msg["content"], (
            "after `learn_remove`, the lesson should no longer "
            "render as a bullet in the system prompt"
        )

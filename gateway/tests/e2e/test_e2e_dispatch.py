"""Judged-e2e dispatch (Phase C task 6) — real pipeline, stubbed model.

Drives build_http_dispatch through the Phase 4.6 e2e app (full chat
pipeline, stubbed LLM) to prove it captures the reply and recovers the
tool_sequence from the persisted turn — no live model needed.
"""

from __future__ import annotations

from typing import Any

from gateway.e2e_driver import build_http_dispatch

from .._fixtures import PERSONAL_TOKEN
from .._llm_stubs import stub_reply, stub_tool_call
from .conftest import StubbedLLM


async def test_dispatch_captures_reply_and_tool_sequence(
    e2e_app: Any, stubbed_llm: StubbedLLM
) -> None:
    # Model calls an auto tool (cron_list, no side effect) then replies.
    stubbed_llm.load([stub_tool_call("cron_list", {}), stub_reply("you have no crons")])
    dispatch = build_http_dispatch(
        e2e_app, alias="fitt-default", token=PERSONAL_TOKEN, session_id="main"
    )
    res = await dispatch([{"role": "user", "content": "list my crons"}])
    assert res.error is None
    assert res.loop_status == "ok"
    assert "no crons" in res.reply
    assert any("cron_list" in t for t in res.tool_sequence)  # from the turn log


async def test_dispatch_multi_turn_returns_last_reply(
    e2e_app: Any, stubbed_llm: StubbedLLM
) -> None:
    stubbed_llm.load([stub_reply("first answer"), stub_reply("second answer")])
    dispatch = build_http_dispatch(
        e2e_app, alias="fitt-default", token=PERSONAL_TOKEN, session_id="main"
    )
    res = await dispatch(
        [
            {"role": "user", "content": "turn one"},
            {"role": "user", "content": "turn two"},
        ]
    )
    assert res.error is None
    assert res.reply == "second answer"  # last turn's reply
    assert res.tool_sequence == ()  # no tools fired


async def test_dispatch_per_turn_session_splits_sessions(
    e2e_app: Any, stubbed_llm: StubbedLLM
) -> None:
    """A turn's ``session`` key runs it in its own session.

    This is what makes cross-session recall testable: state a fact in
    one session, ask in another, where history can't carry it and only
    memory_search can. Without the split, the same-session history makes
    the test pass for the wrong reason.
    """
    stubbed_llm.load([stub_reply("noted"), stub_reply("it was 4821")])
    dispatch = build_http_dispatch(
        e2e_app, alias="fitt-default", token=PERSONAL_TOKEN, session_id="e2e-recall-0"
    )

    res = await dispatch(
        [
            {"role": "user", "content": "note this: 4821", "session": "a"},
            {"role": "user", "content": "what was it?", "session": "b"},
        ]
    )

    assert res.error is None
    assert res.reply == "it was 4821"

    registry = e2e_app.state.session_registry
    assert registry.get("e2e-recall-0-a") is not None
    assert registry.get("e2e-recall-0-b") is not None


async def test_dispatch_strips_the_session_key_from_the_payload(
    e2e_app: Any, stubbed_llm: StubbedLLM
) -> None:
    """``session`` is harness routing, not part of the chat message."""
    stubbed_llm.load([stub_reply("ok")])
    dispatch = build_http_dispatch(
        e2e_app, alias="fitt-default", token=PERSONAL_TOKEN, session_id="e2e-strip-0"
    )

    res = await dispatch([{"role": "user", "content": "hello", "session": "a"}])

    assert res.error is None
    assert res.reply == "ok"

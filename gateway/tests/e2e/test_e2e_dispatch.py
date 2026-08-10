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

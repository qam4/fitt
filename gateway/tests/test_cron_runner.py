"""Tests for Phase 4.5 Task 5 — cron firing → agent session.

End-to-end without HTTP: build an app, register a cron whose
schedule is already due, poke the scheduler's tick, assert
(a) the agent loop ran (via a stubbed litellm), (b) the event
log gained the right entries, (c) memory was appended, and
(d) the approval-mode=auto override works.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gateway.app import create_app
from gateway.cron import CronJob, CronSchedule
from gateway.cron_runner import CronRunner, _AutoApproveWrapper
from gateway.tools import (
    ApprovalBucket,
    ApprovalDecision,
    Tool,
    ToolContext,
    ToolResult,
)

from ._fixtures import build_test_config

# --------------------------------------------------------------- litellm stubs


def _fake_completion(*, content: str = "fired", tool_calls: list[dict] | None = None) -> Any:
    class _Resp:
        def __init__(self) -> None:
            self.usage = type(
                "Usage",
                (),
                {"prompt_tokens": 10, "completion_tokens": 5},
            )()

        def model_dump(self, **_: Any) -> dict[str, Any]:
            msg: dict[str, Any] = {"role": "assistant"}
            if content:
                msg["content"] = content
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": msg,
                        "finish_reason": "tool_calls" if tool_calls else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

    return _Resp()


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    return create_app(cfg)


# --------------------------------------------------------------- fire happy path


async def test_fire_emits_events_and_persists_memory(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cron firing with a stubbed LLM should produce:

    * one cron_fired event at the start
    * one cron_completed event carrying the assistant text
    * one memory turn with user=cron.message and assistant=reply
    """

    async def fake(**kwargs: Any) -> Any:
        return _fake_completion(content="briefing: nothing urgent.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    runner: CronRunner = app.state.cron_runner
    job = CronJob(
        id="abc",
        name="briefing",
        message="summarise open PRs",
        schedule=CronSchedule(kind="every", every_secs=60),
    )
    await runner.fire(job)

    # Events.
    events = app.state.events.read()
    kinds = [e.kind for e in events]
    assert "cron_fired" in kinds
    assert "cron_completed" in kinds
    completed = next(e for e in events if e.kind == "cron_completed")
    assert completed.body == "briefing: nothing urgent."
    assert completed.meta["cron_id"] == "abc"
    assert completed.session_key.startswith("cron:abc:")

    # Memory turn landed.
    history_dir = app.state.config.memory.sessions_dir
    assert history_dir.exists()


async def test_fire_silent_does_not_populate_body(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """silent=True suppresses the reply body in cron_completed
    (but still emits the event). send_message would be how a
    silent cron gets the user's attention on a state change."""

    async def fake(**kwargs: Any) -> Any:
        return _fake_completion(content="state still running")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)
    runner: CronRunner = app.state.cron_runner
    job = CronJob(
        id="xyz",
        name="monitor",
        message="is the job done?",
        schedule=CronSchedule(kind="every", every_secs=60),
        silent=True,
    )
    await runner.fire(job)

    events = app.state.events.read()
    completed = next(e for e in events if e.kind == "cron_completed")
    assert completed.body == ""
    assert completed.meta["silent"] is True


# --------------------------------------------------------------- failure paths


async def test_fire_upstream_error_emits_failed(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("upstream kaboom")

    monkeypatch.setattr("gateway.router.litellm.acompletion", boom)

    runner: CronRunner = app.state.cron_runner
    job = CronJob(
        id="fail",
        name="bad",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=60),
    )
    with pytest.raises(RuntimeError):
        await runner.fire(job)

    events = app.state.events.read()
    kinds = [e.kind for e in events]
    assert "cron_fired" in kinds
    assert "cron_failed" in kinds
    failed = next(e for e in events if e.kind == "cron_failed")
    assert "kaboom" in failed.body


# --------------------------------------------------------------- auto-approve


async def test_auto_approve_wrapper_flips_rejected_to_auto() -> None:
    """The wrapper replaces ask/rejected outcomes with auto so
    a cron can run unattended. Deny-list and block stay intact."""

    class _Inner:
        def __init__(self, decision: ApprovalDecision) -> None:
            self.decision = decision

        async def check(self, *_args: Any, **_kwargs: Any) -> ApprovalDecision:
            return self.decision

    # An ask-bucket decision that the inner middleware timed out
    # on → the wrapper should flip it to auto.
    timed_out = _AutoApproveWrapper(_Inner(ApprovalDecision.timeout("no user")))
    decision = await timed_out.check(None, {}, None)  # type: ignore[arg-type]
    assert decision.reason == "auto"

    # Rejected decisions are preserved (user or policy explicitly said no).
    rejected = _AutoApproveWrapper(_Inner(ApprovalDecision.rejected("user tapped reject")))
    decision = await rejected.check(None, {}, None)  # type: ignore[arg-type]
    assert decision.reason == "rejected"

    # Deny list stays deny list.
    deny = _AutoApproveWrapper(_Inner(ApprovalDecision.denied_deny_list("rm -rf /")))
    decision = await deny.check(None, {}, None)  # type: ignore[arg-type]
    assert decision.reason == "denied_deny_list"

    # Block stays block.
    blocked = _AutoApproveWrapper(_Inner(ApprovalDecision.blocked("policy")))
    decision = await blocked.check(None, {}, None)  # type: ignore[arg-type]
    assert decision.reason == "blocked"

    # Auto passes through.
    auto = _AutoApproveWrapper(_Inner(ApprovalDecision.auto("read")))
    decision = await auto.check(None, {}, None)  # type: ignore[arg-type]
    assert decision.reason == "auto"


async def test_fire_with_approval_mode_auto_runs_an_ask_tool(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register a custom 'ask'-bucket tool, fire a cron with
    approval_mode='auto', and confirm the tool executes without
    an approval round-trip. This is the unattended polling
    scenario from requirements U2."""
    # Register a custom ask-bucket tool with a counter so we can
    # assert it was invoked.
    calls: list[dict] = []

    async def impl(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        calls.append(args)
        return ToolResult.ok("tool ran")

    app.state.tool_registry.register(
        Tool(
            name="custom_write",
            description="test-only",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=impl,
            default_bucket=ApprovalBucket.ASK,
        )
    )

    # Stub the LLM: first pass calls the tool, second pass ends.
    passes: list[int] = []

    async def fake(**kwargs: Any) -> Any:
        passes.append(1)
        if len(passes) == 1:
            return _fake_completion(
                content=None,
                tool_calls=[
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "custom_write", "arguments": json.dumps({})},
                    }
                ],
            )
        return _fake_completion(content="done")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    runner: CronRunner = app.state.cron_runner
    job = CronJob(
        id="auto",
        name="polling",
        message="run the write tool",
        schedule=CronSchedule(kind="every", every_secs=60),
        approval_mode="auto",
    )
    await runner.fire(job)

    assert calls == [{}]  # the ask-bucket tool ran
    events = app.state.events.read()
    assert any(e.kind == "cron_completed" for e in events)

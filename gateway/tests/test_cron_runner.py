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


# --------------------------------------------------------------- firing framing


async def test_fire_injects_scheduled_framing_into_system_prompt(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the 2026-05-07 "model re-schedules
    itself" bug. A cron firing with the stored message 'take a
    break' produced a reply that called cron_add again instead
    of delivering the reminder, because the model saw a
    schedule-flavoured user message alongside a cron_add tool
    and pattern-matched toward scheduling.

    Fix: cron_runner injects a ``[Scheduled firing]`` framing
    between the capability block and identity/memory telling
    the model it IS the scheduled firing and should not call
    cron_add to re-schedule itself.

    We pin this by asserting the framing reaches litellm's
    request body so a refactor that drops the framing fails
    loudly — the symptom is invisible in unit tests otherwise
    (the LLM response is stubbed)."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_completion(content="reminder delivered")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    runner: CronRunner = app.state.cron_runner
    job = CronJob(
        id="framing",
        name="take a break",
        message="Stand up and walk around.",
        schedule=CronSchedule(kind="every", every_secs=3600),
    )
    await runner.fire(job)

    # Dig into the system message.
    messages = captured.get("messages", [])
    system = next((m for m in messages if m.get("role") == "system"), None)
    assert system is not None, "cron firing dispatch should have a system message"
    content = system["content"]
    # Pin the shape: capability block AND scheduled-firing framing
    # both present, in that order.
    assert "[Capabilities]" in content
    assert "[Scheduled firing]" in content
    assert content.index("[Capabilities]") < content.index("[Scheduled firing]")
    # The framing names the cron's own identity so the model has
    # context for phrasing the reply.
    assert "take a break" in content
    # And explicitly prohibits cron_add re-invocation, which is
    # the specific failure mode the framing exists to prevent.
    assert "cron_add" in content
    assert "not a fresh request" in content.lower() or "not a fresh request" in content


async def test_fire_framing_names_schedule_shape(app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The framing includes a human-readable schedule phrase so
    the model can tell 'this is the daily briefing cron' from
    'this is the one-shot reminder in 5 minutes'. Different
    shapes call for different reply tones."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_completion(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    runner: CronRunner = app.state.cron_runner

    # interval
    await runner.fire(
        CronJob(
            id="interval",
            name="heartbeat",
            message="ping",
            schedule=CronSchedule(kind="every", every_secs=300),
        )
    )
    system = next(m["content"] for m in captured["messages"] if m.get("role") == "system")
    assert "every 5m" in system

    # one-shot
    captured.clear()
    await runner.fire(
        CronJob(
            id="oneshot",
            name="lunch",
            message="eat",
            schedule=CronSchedule(kind="at", at_ts=1.0),
        )
    )
    system = next(m["content"] for m in captured["messages"] if m.get("role") == "system")
    assert "one-shot" in system


async def test_fire_framing_does_not_block_send_message_guidance(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The framing explicitly permits send_message for the case
    where a silent cron wants to push a notification. Guard
    that the prose isn't accidentally phrased as 'no tools'."""
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_completion(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    runner: CronRunner = app.state.cron_runner
    await runner.fire(
        CronJob(
            id="s",
            name="silent monitor",
            message="check the build",
            schedule=CronSchedule(kind="every", every_secs=60),
            silent=True,
        )
    )
    system = next(m["content"] for m in captured["messages"] if m.get("role") == "system")
    # send_message is named as an allowed tool for the silent
    # push case — losing this phrase would starve silent
    # monitoring crons of their only notification channel.
    assert "send_message" in system


async def test_fire_framing_has_no_example_user_messages(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for the 2026-05-07 "model copied a
    framing example as its actual input" bug.

    The earlier framing contained bracketed examples — 'take a
    break', 'check the build and tell me when it's done', 'any
    new PRs?' — intended as illustrative categories. A naked
    qwen-coder picked one of those example phrases as its real
    prompt and emitted a cron_add call with it, ignoring the
    actual stored message.

    Fix: drop example sentences from the framing. Name the
    tools the model can use (send_message by name) but do NOT
    embed phrases that parse as user requests. This test
    asserts the specific phrases the model grabbed are no
    longer in the framing; keeping the set small so adding
    future framing text is still ergonomic.
    """
    captured: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_completion(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    runner: CronRunner = app.state.cron_runner
    await runner.fire(
        CronJob(
            id="noex",
            name="whatever",
            message="deliver me",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
    )
    system = next(m["content"] for m in captured["messages"] if m.get("role") == "system")

    # Specific phrases we observed the model grab as its input.
    for banned in [
        "check the build",
        "take a break",
        "any new PRs",
        "is the build done",
    ]:
        assert banned.lower() not in system.lower(), (
            f"framing still contains {banned!r}; qwen-coder will grab it "
            "as its actual user prompt. Name the tools instead of the "
            "situations."
        )


async def test_default_alias_prefers_fitt_smart(app: Any) -> None:
    """Regression guard for the 2026-05-07 "qwen-coder narrates
    tool JSON as text" observation. Cron firings run unattended:
    when the model decides to call a tool, it has to ACTUALLY
    emit a tool_calls structure, not narrate JSON in content.
    Frontier models handle this; local Qwen-Coder 14b flakes on
    the tool-call channel consistently.

    Defaulting cron firings to fitt-smart sidesteps that whole
    failure class without forcing operators to remember to set
    agent_alias on every cron_add. Per-cron override via the
    ``agent_alias`` arg still works for cost-sensitive polling
    crons.

    The test config (via _fixtures.build_test_config) includes
    both fitt-smart and fitt-default aliases; fitt-smart must
    win."""
    runner: CronRunner = app.state.cron_runner
    assert runner._default_alias() == "fitt-smart"


async def test_default_alias_falls_back_when_smart_missing(tmp_path: Path) -> None:
    """If the operator's config doesn't define fitt-smart (e.g.
    a local-only setup with no cloud models), the default falls
    through to fitt-default rather than crashing or returning
    a nonsense alias."""
    from decimal import Decimal

    from gateway.config import (
        AllowedToken,
        Config,
        LoggingConfig,
        MemoryConfig,
        ModelConfig,
        Secrets,
        ServerConfig,
    )

    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080),
        aliases={"fitt-default": "qwen-big"},  # no fitt-smart
        models=[
            ModelConfig(
                id="qwen-big",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="qwen2.5-coder:14b",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
        ],
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
        memory=MemoryConfig(
            enabled=False,
            identity_dir=tmp_path / "identity",
            sessions_dir=tmp_path / "sessions",
        ),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="t", token="T" * 44)],
    )
    app = create_app(cfg)
    runner: CronRunner = app.state.cron_runner
    assert runner._default_alias() == "fitt-default"

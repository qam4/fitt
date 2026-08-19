"""Tests for Phase 4.5 Task 5 — cron firing → agent session.

End-to-end without HTTP: build an app, register a cron whose
schedule is already due, poke the scheduler's tick, assert
(a) the agent loop ran (via a stubbed litellm), (b) the event
log gained the right entries, (c) memory was appended, and
(d) the approval-mode=auto override works.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.app import create_app
from gateway.approval import ApprovalMiddleware
from gateway.cron import CronJob, CronSchedule
from gateway.cron_runner import CronRunner, _AutoApproveWrapper
from gateway.tools import (
    ApprovalBucket,
    Tool,
    ToolContext,
    ToolResult,
)

from ._fixtures import build_test_config
from ._llm_stubs import make_response, make_tool_call

# --------------------------------------------------------------- litellm stubs


def _fake_completion(*, content: str = "fired", tool_calls: list[dict] | None = None) -> Any:
    """Compat shim that delegates to the shared stub library.

    Retained as a thin wrapper so existing tests in this file
    keep working; new tests should import ``make_response`` /
    ``stub_*`` builders from ``_llm_stubs`` directly."""
    return make_response(content=content, tool_calls=tool_calls)


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


async def test_auto_approve_wrapper_collapses_ask_buckets_to_auto() -> None:
    """The wrapper resolves the bucket via the registry (not by
    awaiting the inner middleware) and translates ASK /
    TRUST_SESSION / YOLO directly to AUTO so a cron firing
    doesn't block on a tap that will never come.

    Deny-list and BLOCK still kill the call. Reshaped 2026-05-13
    after the prior implementation was found to await
    ``inner.check`` — which on an ASK bucket blocks for the
    full ``approval_timeout_secs``, locking cron firings for
    the entire timeout per ASK call. Inner-stub-based tests
    no longer apply; we test bucket→decision translation
    directly with a real registry."""
    from gateway.tools import ApprovalBucket as Bucket
    from gateway.tools.registry import ToolPolicy, ToolRegistry

    async def _no_impl(_args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("ran")

    def _mk(name: str, default: Bucket) -> Tool:
        return Tool(
            name=name,
            description="test",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=_no_impl,
            default_bucket=default,
        )

    reg = ToolRegistry(ToolPolicy())
    for tool in (
        _mk("auto_tool", Bucket.AUTO),
        _mk("ask_tool", Bucket.ASK),
        _mk("trust_tool", Bucket.TRUST_SESSION),
        _mk("yolo_tool", Bucket.YOLO),
        _mk("blocked_tool", Bucket.BLOCK),
    ):
        reg.register(tool)

    inner = ApprovalMiddleware(reg)
    wrapper = _AutoApproveWrapper(inner)

    class _DummyCtx:
        client = "cron"
        session_key = "main"

    ctx: Any = _DummyCtx()

    # AUTO passes through.
    d = await wrapper.check(reg.lookup("auto_tool"), {}, ctx)
    assert d.reason == "auto"

    # ASK collapses to auto under cron auto-mode.
    d = await wrapper.check(reg.lookup("ask_tool"), {}, ctx)
    assert d.execute is True
    assert d.reason == "auto"

    # TRUST_SESSION same.
    d = await wrapper.check(reg.lookup("trust_tool"), {}, ctx)
    assert d.execute is True
    assert d.reason == "auto"

    # YOLO same — collapses to auto.
    d = await wrapper.check(reg.lookup("yolo_tool"), {}, ctx)
    assert d.execute is True
    assert d.reason == "auto"

    # BLOCK is preserved — auto-mode doesn't override an
    # explicit operator block.
    d = await wrapper.check(reg.lookup("blocked_tool"), {}, ctx)
    assert d.execute is False
    assert d.reason == "blocked"


async def test_auto_approve_wrapper_preserves_deny_list() -> None:
    """A destructive shell command short-circuits before bucket
    resolution, even under cron auto-mode."""
    from gateway.tools import ApprovalBucket as Bucket
    from gateway.tools.registry import ToolPolicy, ToolRegistry

    async def _no_impl(_args: dict[str, Any], _ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("ran")

    reg = ToolRegistry(ToolPolicy())
    reg.register(
        Tool(
            name="project_shell",
            description="x",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=_no_impl,
            default_bucket=Bucket.AUTO,
            shell_command_for=lambda args: args.get("command", ""),
        )
    )

    inner = ApprovalMiddleware(reg)
    wrapper = _AutoApproveWrapper(inner)

    class _DummyCtx:
        client = "cron"
        session_key = "main"

    ctx: Any = _DummyCtx()
    d = await wrapper.check(
        reg.lookup("project_shell"),
        {"command": "rm -rf /"},
        ctx,
    )
    assert d.execute is False
    assert d.reason == "denied_deny_list"


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
                tool_calls=[make_tool_call("c1", "custom_write", {})],
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
        # The grant is separate from approval_mode on purpose: "don't
        # prompt me" is not "widen what's reachable". This test is about
        # the approval axis, so it grants the surface explicitly.
        extra_tools=["custom_write"],
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


async def test_default_alias_prefers_fitt_default(app: Any) -> None:
    """Pin the 'models are configuration, not architecture'
    principle: cron firings default to whatever the operator
    configured as fitt-default. We deliberately do NOT silently
    upgrade to fitt-smart — the operator's choice wins.

    When the local model doesn't handle tool-calling well (a
    qwen2.5-coder:14b observation), the right fix is to pick
    a better local model or explicitly set agent_alias=fitt-smart
    per-cron, not to hide the issue behind a default that
    routes around the operator's configuration invisibly.
    """
    runner: CronRunner = app.state.cron_runner
    assert runner._default_alias() == "fitt-default"


async def test_default_alias_falls_back_to_first_when_no_fitt_default(
    tmp_path: Path,
) -> None:
    """Unusual config without a fitt-default alias: fall back
    to whatever the first alias in the map is. Covers test
    configs and operators who've renamed the default alias."""
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
        aliases={"my-custom-alias": "qwen-big"},  # no fitt-default
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
    assert runner._default_alias() == "my-custom-alias"


# ------------------------------------------- eval-run approval wiring
#
# Audit finding, 2026-08-13. `create_app` passes the approval middleware
# INTO CronRunner at construction, so the runner holds its own reference.
# The e2e harness swapped `app.state.approval` for an auto-approver and
# never reached the runner — and the `cron_fires` scenario runs a real
# agent session through it. A cron with an unset `approval_mode` (the
# default the model creates) would therefore hit the un-wrapped
# middleware, block for the 10-minute `approval_timeout_secs`, then
# reject — and be reported as a model failure.


def test_the_cron_runner_holds_its_own_approval_reference(app: Any) -> None:
    """The hazard itself, pinned. If this ever stops being true the
    helper below is redundant — but silently fixing it elsewhere must not
    leave the helper looking pointless."""
    runner: CronRunner = app.state.cron_runner

    app.state.approval = "swapped-out"

    assert runner._approval is not app.state.approval


def test_auto_approve_for_eval_reaches_the_cron_runner(app: Any) -> None:
    from gateway.e2e_driver import auto_approve_for_eval

    auto_approve_for_eval(app)

    runner: CronRunner = app.state.cron_runner
    assert isinstance(app.state.approval, _AutoApproveWrapper)
    assert isinstance(runner._approval, _AutoApproveWrapper), (
        "the cron runner still holds the un-wrapped middleware, so a cron "
        "firing would block on an approval no one can tap"
    )
    assert runner._approval is app.state.approval


async def test_an_ask_bucket_tool_in_a_cron_firing_does_not_block_after_the_fix(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour the reference bug caused, end to end.

    An ASK-bucket tool inside a cron whose `approval_mode` is unset must
    run rather than wait for a human. Without the fix this test hangs on
    `approval_timeout_secs` and then records a rejection."""
    from gateway.e2e_driver import auto_approve_for_eval

    ran: list[str] = []

    async def _impl(args: dict, ctx: ToolContext) -> ToolResult:
        ran.append("yes")
        return ToolResult.ok("did it")

    app.state.tool_registry.register(
        Tool(
            name="needs_a_tap",
            description="an ASK-bucket tool",
            schema={"type": "object", "properties": {}},
            callable=_impl,
            default_bucket=ApprovalBucket.ASK,
        )
    )
    auto_approve_for_eval(app)

    calls = iter(
        [
            _fake_completion(tool_calls=[make_tool_call("call-1", "needs_a_tap", {})]),
            _fake_completion(content="done"),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="ask-bucket",
            name="ask",
            message="use the tool",
            schedule=CronSchedule(kind="every", every_secs=60),
            # approval_mode deliberately unset — the default a model creates.
            # Granted, because this test is about the approval path, not the
            # surface. The two are independent by design.
            extra_tools=["needs_a_tap"],
        )
    )

    assert ran == ["yes"], "the ASK-bucket tool never ran; approval blocked the firing"


# ------------------------------------------- the firing is visible to the harness
#
# The `reminder_not_executed` scenario decides "did the firing overreach?"
# by reading snapshot_app()["audit_calls"] and filtering to sessions that
# start with "cron:". That whole chain was unverified: the scenario passed,
# but it had never once gone red for the right reason, so a green result
# might have meant "the firing behaved" or "the harness can't see it".
#
# These pin the link end to end with a stubbed model.


async def test_a_firings_tool_calls_reach_the_audit_snapshot(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool called inside a cron firing must appear in the snapshot with
    a `cron:`-prefixed session, or the scenario is green and blind."""
    from gateway.e2e_driver import auto_approve_for_eval, snapshot_app

    async def _impl(args: dict, ctx: ToolContext) -> ToolResult:
        return ToolResult.ok("did the errand")

    app.state.tool_registry.register(
        Tool(
            name="pretend_shell",
            description="stands in for project_shell",
            schema={"type": "object", "properties": {}},
            callable=_impl,
            default_bucket=ApprovalBucket.AUTO,
        )
    )
    auto_approve_for_eval(app)

    calls = iter(
        [
            _fake_completion(tool_calls=[make_tool_call("c1", "pretend_shell", {})]),
            _fake_completion(content="done"),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="firing1",
            name="reminder",
            message="Remind me to check my emails",
            schedule=CronSchedule(kind="every", every_secs=60),
            # Granted explicitly because firings now run on a reduced
            # surface. This test is about whether the harness can SEE a
            # firing's tool call, so the tool has to be reachable —
            # the restriction itself is pinned separately below.
            extra_tools=["pretend_shell"],
        )
    )

    snap = snapshot_app(app)
    overreach = [
        c
        for c in snap.get("audit_calls", [])
        if c["tool"] == "pretend_shell" and str(c["session"]).startswith("cron:")
    ]

    assert overreach, (
        "a tool called inside the firing did not reach audit_calls with a "
        f"cron: session — the scenario cannot see overreach. saw: {snap.get('audit_calls')}"
    )


async def test_the_session_prefix_the_scenario_filters_on_is_real(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reminder_not_executed` hardcodes the "cron:" prefix. If the runner
    ever changes its session-key format, the scenario would silently stop
    attributing anything to the firing — passing forever."""

    async def fake(**kwargs: Any) -> Any:
        return _fake_completion(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="abc123",
            name="r",
            message="Remind me to check my emails",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
    )

    keys = [str(getattr(e, "session_key", "")) for e in app.state.events.read(limit=20)]
    fired = [k for k in keys if k.startswith("cron:abc123:")]

    assert fired, f"firing sessions no longer look like 'cron:<id>:<ts>': {keys}"


# ------------------------------------------- least privilege for firings
#
# 2026-08-17: "remind me to check my emails in 15 minutes" fired and ran
# project_shell on the operator's hub. The prompt-level fix (make the
# stored text read as a reminder, not an errand) was shipped first and the
# symptom recurred on 2026-08-19 with correctly-authored text. So the
# surface is bounded: a firing gets FIRING_DEFAULT_TOOLS plus whatever the
# cron explicitly grants.
#
# The order these assert in matters. Each one names the best behaviour
# first, then checks the assert scores THAT as a pass.


def _register_pretend_shell(app: Any, ran: list[str]) -> None:
    """A stand-in for project_shell that records being called.

    Named differently from the real tool so these tests don't depend on
    whether project_shell happens to be registered in the test config."""

    async def _impl(args: dict, ctx: ToolContext) -> ToolResult:
        ran.append("called")
        return ToolResult.ok("ran a command")

    app.state.tool_registry.register(
        Tool(
            name="pretend_shell",
            description="stands in for project_shell",
            schema={"type": "object", "properties": {}},
            callable=_impl,
            default_bucket=ApprovalBucket.AUTO,
        )
    )


async def test_a_firing_cannot_run_an_ungranted_tool(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported bug. Best behaviour: the model asks for the shell, the
    gateway refuses, nothing runs. So: the callable must not fire."""
    ran: list[str] = []
    _register_pretend_shell(app, ran)

    calls = iter(
        [
            _fake_completion(tool_calls=[make_tool_call("c1", "pretend_shell", {})]),
            _fake_completion(content="Reminder: check your emails."),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="nogrant",
            name="reminder",
            message="Remind me to check my emails",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
    )

    assert ran == [], "an ungranted tool ran inside a cron firing"


async def test_a_grant_makes_the_tool_reachable_again(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half. Least privilege that can't be widened would just
    break monitoring crons, so the grant has to actually work."""
    ran: list[str] = []
    _register_pretend_shell(app, ran)

    calls = iter(
        [
            _fake_completion(tool_calls=[make_tool_call("c1", "pretend_shell", {})]),
            _fake_completion(content="build is green"),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="granted",
            name="build watch",
            message="Check whether the build is green and tell me.",
            schedule=CronSchedule(kind="every", every_secs=60),
            extra_tools=["pretend_shell"],
        )
    )

    assert ran == ["called"], "an explicitly granted tool was still withheld"


async def test_a_withheld_tool_is_not_reported_as_hallucinated(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the model is TOLD matters as much as what it can do. Calling a
    withheld tool "likely a hallucinated call" is a lie, and it hides the
    operator-actionable fact that a grant is missing (Principles 8 + 11).

    Best behaviour: the error names the restriction and the grant. So the
    assert looks for that, and for the absence of the hallucination line."""
    ran: list[str] = []
    _register_pretend_shell(app, ran)

    seen: list[dict[str, Any]] = []
    calls = iter(
        [
            _fake_completion(tool_calls=[make_tool_call("c1", "pretend_shell", {})]),
            _fake_completion(content="I couldn't do that part."),
        ]
    )

    async def fake(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return next(calls)

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="explain",
            name="reminder",
            message="Remind me to check my emails",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
    )

    # The tool result is fed back as a message on the second call.
    tool_msgs = [
        m for kwargs in seen for m in kwargs.get("messages", []) if m.get("role") == "tool"
    ]
    blob = " ".join(str(m.get("content", "")) for m in tool_msgs)

    assert tool_msgs, f"no tool result was fed back to the model: {seen}"
    assert "hallucinat" not in blob.lower(), f"a withheld tool was blamed on the model: {blob}"
    assert "extra_tools" in blob, f"the error does not name the grant needed: {blob}"


async def test_the_capability_block_a_firing_reads_omits_withheld_tools(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restricting the wire surface but still advertising the tool would be
    an invitation to fail. The system prompt and the tools array both come
    off the same restricted view, so check the prompt the model saw."""
    ran: list[str] = []
    _register_pretend_shell(app, ran)

    seen: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return _fake_completion(content="Reminder: check your emails.")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    await app.state.cron_runner.fire(
        CronJob(
            id="prompt",
            name="reminder",
            message="Remind me to check my emails",
            schedule=CronSchedule(kind="every", every_secs=60),
        )
    )

    system = next(m["content"] for m in seen[0]["messages"] if m.get("role") == "system")
    wire_tools = {t["function"]["name"] for t in (seen[0].get("tools") or []) if "function" in t}

    assert "pretend_shell" not in system, (
        "the capability block advertises a tool the firing cannot call"
    )
    assert "pretend_shell" not in wire_tools
    # send_message is the whole point of a scheduled job; it must survive.
    assert "send_message" in system or "send_message" in wire_tools


def test_firing_defaults_name_only_tools_that_actually_exist(app: Any) -> None:
    """A typo in FIRING_DEFAULT_TOOLS would silently narrow the surface —
    the allow-list ignores unknown names by design, so nothing would
    complain. Anchor it against the live registry.

    Tools registered conditionally (memory_search needs retrieval; the
    project tools need a project registry) are exempted, since their
    absence is a deployment fact, not a typo."""
    from gateway.cron_runner import FIRING_DEFAULT_TOOLS

    registered = set(app.state.tool_registry.list_names())
    conditional = {"memory_search"}
    missing = (FIRING_DEFAULT_TOOLS - registered) - conditional

    assert not missing, (
        f"FIRING_DEFAULT_TOOLS names tools that are not registered: {sorted(missing)}"
    )

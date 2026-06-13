"""Tests for Phase 12 task 10 — the orchestrator (plan -> execute).

The orchestrator is a drop-in for run_agent_loop: messages in,
AgentLoopResult out. A sequenced router serves the planner pass's
responses first, then the executor pass's. Covers the
plan-then-execute path (incl. C1 re-injection into the execute pass's
system message), the elected-no-plan path, todo completion across the
passes, and token aggregation across both passes.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from gateway.agent_loop import AgentLoopResult
from gateway.config import ModelConfig
from gateway.orchestrator import run_orchestrated_turn
from gateway.plan_store import PlanStore
from gateway.projects import ProjectRegistry
from gateway.prompt_resolver import PromptResolver
from gateway.router import DispatchResult
from gateway.tools import (
    ApprovalBucket,
    ApprovalDecision,
    Tool,
    ToolContext,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
    build_plan_tools,
)

_MODEL = ModelConfig(
    id="m",
    backend="openai",
    endpoint="https://example/v1",
    model="fake/model",
    cost_per_mtok_in=Decimal("0"),
    cost_per_mtok_out=Decimal("0"),
)


def _resp(
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    else:
        msg["content"] = content or ""
    return {
        "id": "r",
        "choices": [
            {
                "index": 0,
                "message": msg,
                "finish_reason": "tool_calls" if tool_calls is not None else "stop",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 10},
    }


class _SeqRouter:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.bodies: list[dict[str, Any]] = []
        self.aliases: list[str] = []

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        self.bodies.append(body)
        self.aliases.append(alias)
        resp = self._responses.pop(0)
        return DispatchResult(response=resp, stream=None, model_used=_MODEL, fallback_used=False)

    def resolve(self, alias: str) -> list[ModelConfig]:
        return [_MODEL]


class _AutoApprove:
    async def check(self, tool: Any, args: Any, ctx: Any) -> ApprovalDecision:
        return ApprovalDecision.auto()


async def _noop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult.ok("ok")


def _registry() -> ToolRegistry:
    reg = ToolRegistry(ToolPolicy.from_config(None))
    for t in build_plan_tools():
        reg.register(t)
    reg.register(
        Tool(
            name="noop",
            description="does nothing",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=_noop,
            default_bucket=ApprovalBucket.AUTO,
        )
    )
    return reg


def _ctx(store: PlanStore) -> ToolContext:
    return ToolContext(
        client="cli",
        session_key="main",
        projects=ProjectRegistry(Path("nonexistent.yaml")),
        plan_store=store,
    )


def _todowrite_call(todos: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "c1",
        "type": "function",
        "function": {"name": "todowrite", "arguments": json.dumps({"todos": todos})},
    }


async def _run(store: PlanStore, router: _SeqRouter, msg: str) -> AgentLoopResult:
    return await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": msg}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )


async def test_orchestrator_plans_then_executes() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner pass: emit a plan (one dispatch — default budget 1)
            _resp(tool_calls=[_todowrite_call([{"text": "search news"}, {"text": "summarise"}])]),
            # executor pass: produce the answer
            _resp(content="here is your summary"),
        ]
    )
    result = await _run(store, router, "summarise today's news")
    # Drop-in shape: AgentLoopResult with the executor's reply.
    assert isinstance(result, AgentLoopResult)
    assert result.status == "ok"
    assert result.assistant_text == "here is your summary"
    # Plan landed in the store.
    plan = store.get("main")
    assert plan is not None
    assert [i.text for i in plan.items] == ["search news", "summarise"]
    # C1: the execute pass (last dispatch) re-injected the plan into its system message.
    exec_body = router.bodies[-1]
    assert exec_body["messages"][0]["role"] == "system"
    assert "[Plan]" in exec_body["messages"][0]["content"]
    assert "search news" in exec_body["messages"][0]["content"]
    # Tokens summed across planner (1 dispatch) + executor (1).
    assert result.in_tokens == 10
    assert result.out_tokens == 20


async def test_orchestrator_elects_no_plan_single_action() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="single step, no plan needed"),  # planner elects out
            _resp(content="it is 3pm"),  # executor answers
        ]
    )
    result = await _run(store, router, "what time is it")
    assert result.assistant_text == "it is 3pm"
    assert store.get("main") is None
    # No plan and empty execute prompt -> no system message added; the
    # execute pass sees the bare user turn.
    exec_body = router.bodies[-1]
    assert exec_body["messages"][0] == {"role": "user", "content": "what time is it"}
    assert result.in_tokens == 10
    assert result.out_tokens == 20


async def test_orchestrator_executor_completes_plan() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner: one-step plan (one dispatch — default budget 1)
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "do it"}])]),
            # executor: tick the step done, then answer
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "do it", "status": "done"}])]),
            _resp(content="finished"),
        ]
    )
    result = await _run(store, router, "do it")
    assert result.assistant_text == "finished"
    plan = store.get("main")
    assert plan is not None
    assert plan.is_complete() is True
    assert plan.items[0].status == "done"


# --------------------------------------------------------------- task 11


async def test_orchestrator_planner_alias_routes_plan_pass() -> None:
    """planner_alias runs the plan pass on a different alias than the
    executor pass (Story 2.2): plan with a capable model, execute with
    a fast one."""
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner pass (on the planner alias): emit a plan
            _resp(tool_calls=[_todowrite_call([{"text": "step one"}])]),
            # executor pass (on the turn's own alias): answer
            _resp(content="done"),
        ]
    )
    result = await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "do a thing"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        planner_alias="fitt-cloud-smart",
        planner_max_iterations=1,
    )
    assert result.assistant_text == "done"
    # First dispatch (planner pass) used the planner alias; the last
    # (executor pass) used the turn's own alias.
    assert router.aliases[0] == "fitt-cloud-smart"
    assert router.aliases[-1] == "fitt-local-qwen3"


async def test_orchestrator_planner_budget_one_stops_after_first_dispatch() -> None:
    """planner_max_iterations=1 caps the plan pass at a single model
    request — the plan is captured on the first todowrite and no second
    planner dispatch is made (keeps a cloud planner under RPM)."""
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner pass: emit a plan on the first (only) dispatch
            _resp(tool_calls=[_todowrite_call([{"text": "only step"}])]),
            # executor pass: answer
            _resp(content="answer"),
        ]
    )
    result = await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "go"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        planner_max_iterations=1,
    )
    assert result.assistant_text == "answer"
    # Exactly two dispatches total: one planner (budget=1), one executor.
    assert len(router.aliases) == 2
    plan = store.get("main")
    assert plan is not None
    assert [i.text for i in plan.items] == ["only step"]


# --------------------------------------------------------------- task 14 (recovery)


def _noop_call(cid: str = "n1") -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": "noop", "arguments": "{}"},
    }


async def test_recovery_not_triggered_on_clean_turn() -> None:
    """A clean turn (planner elects out, executor answers directly)
    runs no recovery re-runs: exactly planner + executor dispatches."""
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="no plan needed"),  # planner elects out
            _resp(content="the answer"),  # executor: clean, no tools
        ]
    )
    result = await _run(store, router, "easy question")
    assert result.assistant_text == "the answer"
    assert result.status == "ok"
    assert len(router.aliases) == 2  # no recovery re-run


async def test_recovery_nudge_recovers_empty_after_tools() -> None:
    """empty-after-tools triggers a continue-nudge; the re-run produces
    content and the turn recovers."""
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="no plan"),  # planner elects out
            _resp(tool_calls=[_noop_call()]),  # executor runs a tool
            _resp(content=""),  # ...then returns an empty reply
            _resp(content="recovered answer"),  # nudge re-run answers
        ]
    )
    result = await _run(store, router, "do a thing")
    assert result.assistant_text == "recovered answer"
    assert result.status == "ok"
    assert len(router.aliases) == 4  # planner + exec(2) + nudge(1)
    # The nudge re-run carried the recover-step prompt as a system msg.
    recover_prompt = PromptResolver().resolve("recover", "fitt-local-qwen3")
    nudge_body = router.bodies[-1]
    assert any(
        m.get("role") == "system" and recover_prompt in m.get("content", "")
        for m in nudge_body["messages"]
    )


async def test_recovery_replan_uses_clean_context() -> None:
    """budget exhaustion re-plans on a clean context: the re-run's
    messages drop the flailing transcript (no assistant tool_calls
    carried over), keeping only goal + recover prompt (Story 5.3)."""
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="no plan"),  # planner elects out
            _resp(tool_calls=[_noop_call("a")]),  # exec iter 1
            _resp(tool_calls=[_noop_call("b")]),  # exec iter 2 -> budget exhausted
            _resp(content="done after replan"),  # replan re-run answers
        ]
    )
    result = await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "loop then recover"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        planner_max_iterations=1,
        executor_max_iterations=2,
    )
    assert result.assistant_text == "done after replan"
    assert result.status == "ok"
    replan_body = router.bodies[-1]
    # Clean context: no assistant tool_calls message carried forward.
    assert not any(
        m.get("role") == "assistant" and m.get("tool_calls") for m in replan_body["messages"]
    )


async def test_recovery_honest_stop_when_trouble_persists() -> None:
    """When the trouble keeps recurring through nudge and re-plan, the
    turn ends with an honest stop report (status ok, no fabrication)."""
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="no plan"),  # planner elects out
            _resp(tool_calls=[_noop_call()]),  # exec tool
            _resp(content=""),  # exec empty -> trouble
            _resp(tool_calls=[_noop_call()]),  # nudge tool
            _resp(content=""),  # nudge empty -> trouble again
            _resp(tool_calls=[_noop_call()]),  # replan tool
            _resp(content=""),  # replan empty -> trouble again -> stop
        ]
    )
    result = await _run(store, router, "stubborn task")
    assert result.status == "ok"
    assert "stopping" in result.assistant_text.lower()
    assert "empty" in result.assistant_text.lower()  # the observed fact
    assert len(router.aliases) == 7  # planner + exec(2) + nudge(2) + replan(2)


# --------------------------------------------------------------- task 15 (capability gap)


async def test_capability_gap_is_terminal_not_recovered() -> None:
    """A capability-gap reply ('I'd need a tool to X') is a terminal
    honest outcome (Story 4.4): even though the preceding tool call
    errored (a trouble signal), recovery must NOT fire — the gap reply
    is delivered as-is."""
    store = PlanStore()
    gap_reply = "I'd need a tool to send email to do that. Consider adding send_email."
    router = _SeqRouter(
        [
            _resp(content="no plan"),  # planner elects out
            _resp(tool_calls=[_noop_call()]),  # executor tries a tool...
            _resp(content=gap_reply),  # ...then honestly reports a gap
        ]
    )
    result = await _run(store, router, "email my boss")
    assert result.assistant_text == gap_reply
    assert result.status == "ok"
    # No recovery re-run: planner + executor(2) only.
    assert len(router.aliases) == 3


async def test_capability_gap_after_tool_error_not_recovered() -> None:
    """Same, but the last tool call errored — the trouble detector
    would see tool_error, yet the gap reply preempts recovery."""
    store = PlanStore()

    async def _fail(args: Any, ctx: ToolContext) -> ToolResult:
        return ToolResult.error("nope")

    reg = _registry()
    reg.register(
        Tool(
            name="flaky",
            description="fails",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=_fail,
            default_bucket=ApprovalBucket.AUTO,
        )
    )
    gap_reply = "I'd need a tool to query the database here."
    router = _SeqRouter(
        [
            _resp(content="no plan"),
            _resp(
                tool_calls=[
                    {
                        "id": "f1",
                        "type": "function",
                        "function": {"name": "flaky", "arguments": "{}"},
                    }
                ]
            ),
            _resp(content=gap_reply),
        ]
    )
    result = await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "read the db"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=reg,
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        planner_max_iterations=1,
    )
    assert result.assistant_text == gap_reply
    assert len(router.aliases) == 3  # no recovery despite the tool error


# --------------------------------------------------------------- task 17 (plan events)


def _ctx_with_turns(store: PlanStore, log: Any, turn_id: str = "t1") -> ToolContext:
    return ToolContext(
        client="cli",
        session_key="main",
        projects=ProjectRegistry(Path("nonexistent.yaml")),
        plan_store=store,
        turns=log,
        turn_id=turn_id,
    )


def _kinds(log: Any) -> list[str]:
    return [e.kind for e in _events(log)]


def _events(log: Any) -> list[Any]:
    import time as _time

    return log.read("main", now=_time.time())


async def test_emits_plan_created_and_step_completed(tmp_path: Path) -> None:
    from gateway.turns import TurnLog

    log = TurnLog(tmp_path)
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner: a one-step plan (created pending)
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "do it"}])]),
            # executor: tick it done, then answer
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "do it", "status": "done"}])]),
            _resp(content="finished"),
        ]
    )
    await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "do it"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx_with_turns(store, log),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    events = _events(log)
    kinds = [e.kind for e in events]
    assert "plan_created" in kinds
    assert "plan_step_completed" in kinds
    created = next(e for e in events if e.kind == "plan_created")
    assert created.meta["item_count"] == 1
    completed = next(e for e in events if e.kind == "plan_step_completed")
    assert completed.meta["step_id"] == "1"


async def test_no_plan_created_when_elected_out(tmp_path: Path) -> None:
    from gateway.turns import TurnLog

    log = TurnLog(tmp_path)
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="no plan needed"),  # planner elects out
            _resp(content="the answer"),  # executor
        ]
    )
    await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "easy"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx_with_turns(store, log),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    assert "plan_created" not in _kinds(log)


async def test_emits_replan_event(tmp_path: Path) -> None:
    from gateway.turns import TurnLog

    log = TurnLog(tmp_path)
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="no plan"),  # planner elects out
            _resp(tool_calls=[_noop_call("a")]),  # exec iter 1
            _resp(tool_calls=[_noop_call("b")]),  # exec iter 2 -> budget exhausted
            _resp(content="done after replan"),  # replan re-run answers
        ]
    )
    await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        messages=[{"role": "user", "content": "loop then recover"}],
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx_with_turns(store, log),
        prompt_resolver=PromptResolver(),
        session_key="main",
        planner_max_iterations=1,
        executor_max_iterations=2,
    )
    kinds = _kinds(log)
    assert "replan" in kinds
    replan = next(e for e in _events(log) if e.kind == "replan")
    assert "budget_exhausted" in replan.meta["reason"]

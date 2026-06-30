"""Tests for Phase 12 task 8 — the planner pass.

Wiring tested with fakes (no real model): a stub router returns a
``todowrite`` tool_call, the loop executes it (auto-approved), and the
plan lands in the PlanStore. Also the elected-skip path (model
replies without planning) and that the plan-step prompt is what's
sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from gateway.config import ModelConfig
from gateway.errors import NoBackendAvailable
from gateway.plan_store import PlanStore
from gateway.planner import measure_plan_election, run_planner_pass
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
    reasoning: str | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
        msg["content"] = None
    else:
        msg["content"] = content or ""
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
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
    """Returns canned responses in order; captures dispatched bodies."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.bodies: list[dict[str, Any]] = []

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        self.bodies.append(body)
        resp = self._responses.pop(0)
        return DispatchResult(response=resp, stream=None, model_used=_MODEL, fallback_used=False)

    def resolve(self, alias: str) -> list[ModelConfig]:
        return [_MODEL]


class _AutoApprove:
    async def check(self, tool: Any, args: Any, ctx: Any) -> ApprovalDecision:
        return ApprovalDecision.auto()


def _registry() -> ToolRegistry:
    reg = ToolRegistry(ToolPolicy.from_config(None))
    for t in build_plan_tools():
        reg.register(t)
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


async def test_planner_writes_plan_when_model_elects() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(tool_calls=[_todowrite_call([{"text": "search news"}, {"text": "summarise"}])]),
            _resp(content="plan ready"),
        ]
    )
    result = await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="summarise today's news",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    assert result.planned is True
    assert result.plan is not None
    assert [i.text for i in result.plan.items] == ["search news", "summarise"]
    # Persisted to the store under the session.
    assert store.get("main") == result.plan


async def test_planner_elects_not_to_plan() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="that's a single step; no plan needed")])
    result = await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="what time is it",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    assert result.planned is False
    assert result.plan is None
    assert store.get("main") is None


async def test_planner_sends_plan_step_prompt_and_offers_todowrite() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="ok")])
    resolver = PromptResolver()
    await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="do a thing",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=resolver,
        session_key="main",
    )
    body = router.bodies[0]
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == resolver.resolve("plan", "fitt-local-qwen3")
    assert body["messages"][1] == {"role": "user", "content": "do a thing"}
    offered = [t["function"]["name"] for t in body["tools"]]
    assert offered == ["todowrite"]


# --------------------------------------------------------------- executor-tool-visibility hint


async def _noop_tool(args: Any, ctx: ToolContext) -> ToolResult:
    return ToolResult.ok("ok")


async def test_planner_injects_executor_tool_hint() -> None:
    """When the registry carries executor tools beyond todowrite, the plan
    prompt is augmented with a hint naming them (validated to lift
    plan-election on a capable model). The planner still OFFERS only
    todowrite."""
    store = PlanStore()
    reg = _registry()  # has todowrite
    reg.register(
        Tool(
            name="web_search",
            description="search the web for current information",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            callable=_noop_tool,
            default_bucket=ApprovalBucket.AUTO,
        )
    )
    router = _SeqRouter([_resp(content="ok")])
    await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="summarise the news",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=reg,
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    sys_msg = router.bodies[0]["messages"][0]["content"]
    assert "execution step" in sys_msg.lower()
    assert "web_search" in sys_msg
    # todowrite is the planner's own tool, not listed as an executor tool.
    assert "- todowrite:" not in sys_msg
    # Only todowrite is actually offered for calling.
    offered = [t["function"]["name"] for t in router.bodies[0]["tools"]]
    assert offered == ["todowrite"]


async def test_planner_no_hint_when_only_todowrite() -> None:
    """With no executor tools registered, the plan prompt is unchanged."""
    store = PlanStore()
    router = _SeqRouter([_resp(content="ok")])
    resolver = PromptResolver()
    await run_planner_pass(
        alias="fitt-local-qwen3",
        user_message="x",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=resolver,
        session_key="main",
    )
    sys_msg = router.bodies[0]["messages"][0]["content"]
    assert sys_msg == resolver.resolve("plan", "fitt-local-qwen3")


# --------------------------------------------------------------- task 14b: thinking-model nudge


async def test_planner_nudge_recovers_thinking_stall() -> None:
    """A thinking model returns empty content + reasoning_content and no
    todowrite (the loop reads it as done). The planner-level nudge
    re-prompts once and the model then emits the plan."""
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner pass: thinking stall (reasoned, no tool call)
            _resp(content="", reasoning="I'll search the web then summarise."),
            # nudge pass: now emits the plan, then a natural-stop reply
            _resp(tool_calls=[_todowrite_call([{"text": "search"}, {"text": "summarise"}])]),
            _resp(content="planned"),
        ]
    )
    result = await run_planner_pass(
        alias="fitt-ec2-qwen3",
        user_message="summarise today's news",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        max_iterations=1,
    )
    assert result.planned is True
    assert [i.text for i in result.plan.items] == ["search", "summarise"]  # type: ignore[union-attr]
    # planner pass (1 dispatch) + nudge pass (2 dispatches).
    assert len(router.bodies) == 3
    # The nudge dispatch carried the model's own reasoning back + the nudge.
    nudge_body = router.bodies[1]
    contents = [m.get("content", "") for m in nudge_body["messages"]]
    assert any("search the web then summarise" in c for c in contents)  # reasoning fed back
    assert any("todowrite" in c for c in contents)  # the nudge text


async def test_planner_stall_by_tokens_without_reasoning_is_nudged() -> None:
    """Empty content + no tool call + nonzero completion tokens (no
    reasoning_content field) still counts as a stall and is nudged."""
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content=""),  # empty, no reasoning, but out_tokens>0
            _resp(tool_calls=[_todowrite_call([{"text": "x"}])]),
            _resp(content="ok"),
        ]
    )
    result = await run_planner_pass(
        alias="fitt-ec2-qwen3",
        user_message="do a multi-step thing",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        max_iterations=1,
    )
    assert result.planned is True
    assert len(router.bodies) == 3


async def test_planner_elect_out_is_not_nudged() -> None:
    """A genuine elect-out (non-empty content, no tool call) is NOT a
    stall — the nudge must not fire."""
    store = PlanStore()
    router = _SeqRouter([_resp(content="single step; no plan needed")])
    result = await run_planner_pass(
        alias="fitt-ec2-qwen3",
        user_message="what time is it",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        max_iterations=1,
    )
    assert result.planned is False
    assert len(router.bodies) == 1  # no nudge re-run


async def test_planner_nudge_can_be_disabled() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="", reasoning="thinking but no action")])
    result = await run_planner_pass(
        alias="fitt-ec2-qwen3",
        user_message="do a thing",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
        max_iterations=1,
        nudge_on_stall=False,
    )
    assert result.planned is False
    assert len(router.bodies) == 1


# --------------------------------------------------------------- plan-election measurement


def _ctx_factory(store: PlanStore) -> Callable[[str], ToolContext]:
    """A make_tool_ctx factory mirroring the profiler/scenario wiring:
    each session_key gets its own ToolContext over a shared PlanStore
    (the store is session-keyed, so samples stay independent)."""

    def _make(session_key: str) -> ToolContext:
        return ToolContext(
            client="cli",
            session_key=session_key,
            projects=ProjectRegistry(Path("nonexistent.yaml")),
            plan_store=store,
        )

    return _make


class _RaisingRouter:
    """Dispatch always raises NoBackendAvailable (a dropped backend)."""

    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        self.calls += 1
        raise NoBackendAvailable("backend dropped mid-run")

    def resolve(self, alias: str) -> list[ModelConfig]:
        return [_MODEL]


async def test_measure_plan_election_counts_elections() -> None:
    """Three samples (elect, elect, elect-out) give passes=2 over valid=3,
    with cost lists populated for every valid sample."""
    store = PlanStore()
    router = _SeqRouter(
        [
            # sample 1: emits a plan, then a natural-stop reply
            _resp(tool_calls=[_todowrite_call([{"text": "search"}])]),
            _resp(content="planned"),
            # sample 2: emits a plan, then a reply
            _resp(tool_calls=[_todowrite_call([{"text": "summarise"}])]),
            _resp(content="planned"),
            # sample 3: elects not to plan
            _resp(content="single step; no plan needed"),
        ]
    )
    result = await measure_plan_election(
        alias="fitt-ec2-qwen3",
        user_message="summarise today's news",
        samples=3,
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        make_tool_ctx=_ctx_factory(store),
        prompt_resolver=PromptResolver(),
    )
    assert result.total == 3
    assert result.transient == 0
    assert result.valid == 3
    assert result.passes == 2
    # cost lists carry one entry per valid sample (capability + cost).
    assert len(result.latencies_ms) == 3
    assert len(result.in_tokens) == 3
    assert len(result.out_tokens) == 3


async def test_measure_plan_election_excludes_transient() -> None:
    """A dropped backend is recorded transient and excluded from the
    denominator (multi-sample convention 3): valid=0, no signal."""
    store = PlanStore()
    result = await measure_plan_election(
        alias="fitt-ec2-qwen3",
        user_message="summarise today's news",
        samples=2,
        alias_router=_RaisingRouter(),  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        make_tool_ctx=_ctx_factory(store),
        prompt_resolver=PromptResolver(),
    )
    assert result.total == 2
    assert result.transient == 2
    assert result.valid == 0
    assert result.passes == 0
    assert result.latencies_ms == []
    assert result.in_tokens == []


async def test_measure_plan_election_feeds_grade() -> None:
    """The aggregate plugs straight into grade_from_samples to become the
    profiler's ``plan-election`` MeasuredGrade (pass_rate over valid)."""
    from gateway.capability_profile import grade_from_samples

    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(tool_calls=[_todowrite_call([{"text": "a"}])]),
            _resp(content="planned"),
            _resp(content="no plan needed"),
        ]
    )
    result = await measure_plan_election(
        alias="fitt-ec2-qwen3",
        user_message="summarise today's news",
        samples=2,
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        make_tool_ctx=_ctx_factory(store),
        prompt_resolver=PromptResolver(),
    )
    grade = grade_from_samples(
        "plan-election",
        passes=result.passes,
        valid=result.valid,
        samples=result.total,
        latencies_ms=result.latencies_ms,
        in_tokens=result.in_tokens,
        out_tokens=result.out_tokens,
    )
    assert grade.name == "plan-election"
    assert grade.pass_rate == 0.5
    assert grade.passes == 1
    assert grade.valid == 2

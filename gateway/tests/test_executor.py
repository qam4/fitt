"""Tests for Phase 12 task 9 — the executor pass.

Wiring tested with fakes (no real model). Covers:

* C1 re-injection — a plan in the store is rendered into the execute
  pass's system message.
* the no-plan plain path — empty store runs as an ordinary loop with
  no system message (default execute prompt is empty).
* full-toolset offering — the executor offers every registered tool,
  not just ``todowrite`` (the planner's restriction).
* todo ticking — the executor's ``todowrite`` call updates the stored
  plan, and ``plan_complete`` reflects it.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from gateway.config import ModelConfig
from gateway.executor import run_executor_pass
from gateway.plan_store import Plan, PlanItem, PlanStore
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


async def _noop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult.ok("ok")


def _registry() -> ToolRegistry:
    reg = ToolRegistry(ToolPolicy.from_config(None))
    for t in build_plan_tools():
        reg.register(t)
    # A second tool so "offers the full toolset" is a meaningful assertion.
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


async def test_executor_reinjects_plan_into_system_message() -> None:
    store = PlanStore()
    store.set(
        "main",
        Plan(items=[PlanItem("1", "search news", "pending"), PlanItem("2", "summarise", "done")]),
    )
    router = _SeqRouter([_resp(content="working on it")])
    await run_executor_pass(
        alias="fitt-local-qwen3",
        user_message="summarise today's news",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    body = router.bodies[0]
    sys_msg = body["messages"][0]
    assert sys_msg["role"] == "system"
    assert "[Plan]" in sys_msg["content"]
    assert "search news" in sys_msg["content"]
    assert "summarise" in sys_msg["content"]
    assert body["messages"][-1] == {"role": "user", "content": "summarise today's news"}


async def test_executor_no_plan_runs_plain() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="done")])
    result = await run_executor_pass(
        alias="fitt-local-qwen3",
        user_message="what time is it",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    body = router.bodies[0]
    # Default execute prompt is empty and there's no plan -> no system message.
    assert body["messages"][0] == {"role": "user", "content": "what time is it"}
    assert result.plan is None
    assert result.plan_complete is False


async def test_executor_offers_full_toolset() -> None:
    store = PlanStore()
    router = _SeqRouter([_resp(content="ok")])
    await run_executor_pass(
        alias="fitt-local-qwen3",
        user_message="do a thing",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    offered = {t["function"]["name"] for t in router.bodies[0]["tools"]}
    assert {"todowrite", "noop"} <= offered


async def test_executor_ticks_todos_and_reports_complete() -> None:
    store = PlanStore()
    store.set("main", Plan(items=[PlanItem("1", "step a", "pending")]))
    router = _SeqRouter(
        [
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "step a", "status": "done"}])]),
            _resp(content="all done"),
        ]
    )
    result = await run_executor_pass(
        alias="fitt-local-qwen3",
        user_message="do step a",
        alias_router=router,  # type: ignore[arg-type]
        tool_registry=_registry(),
        approval=_AutoApprove(),
        tool_ctx=_ctx(store),
        prompt_resolver=PromptResolver(),
        session_key="main",
    )
    assert result.plan is not None
    assert result.plan.items[0].status == "done"
    assert result.plan_complete is True

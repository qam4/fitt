"""Tests for Phase 12 task 10 — the orchestrator (plan -> execute).

Wiring tested with fakes: a sequenced router serves the planner pass's
responses first, then the executor pass's. Covers the plan-then-execute
path, the elected-no-plan path, todo completion across the passes, and
token aggregation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

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


async def _run(store: PlanStore, router: _SeqRouter, msg: str):  # type: ignore[no-untyped-def]
    return await run_orchestrated_turn(
        alias="fitt-local-qwen3",
        user_message=msg,
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
            # planner pass: emit a plan, then stop
            _resp(tool_calls=[_todowrite_call([{"text": "search news"}, {"text": "summarise"}])]),
            _resp(content="plan ready"),
            # executor pass: produce the answer
            _resp(content="here is your summary"),
        ]
    )
    result = await _run(store, router, "summarise today's news")
    assert result.planned is True
    assert result.plan is not None
    assert [i.text for i in result.plan.items] == ["search news", "summarise"]
    assert result.assistant_text == "here is your summary"
    assert result.status == "ok"
    # 3 dispatches, each usage 5/10
    assert result.in_tokens == 15
    assert result.out_tokens == 30


async def test_orchestrator_elects_no_plan_single_action() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            _resp(content="single step, no plan needed"),  # planner elects out
            _resp(content="it is 3pm"),  # executor answers
        ]
    )
    result = await _run(store, router, "what time is it")
    assert result.planned is False
    assert result.plan is None
    assert result.plan_complete is False
    assert result.assistant_text == "it is 3pm"
    assert result.in_tokens == 10
    assert result.out_tokens == 20


async def test_orchestrator_executor_completes_plan() -> None:
    store = PlanStore()
    router = _SeqRouter(
        [
            # planner: one-step plan
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "do it"}])]),
            _resp(content="planned"),
            # executor: tick the step done, then answer
            _resp(tool_calls=[_todowrite_call([{"id": "1", "text": "do it", "status": "done"}])]),
            _resp(content="finished"),
        ]
    )
    result = await _run(store, router, "do it")
    assert result.planned is True
    assert result.plan_complete is True
    assert result.plan is not None
    assert result.plan.items[0].status == "done"
    assert result.assistant_text == "finished"

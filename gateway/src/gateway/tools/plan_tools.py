"""Phase 12 task 7 — the ``todowrite`` plan tool.

The model calls ``todowrite`` to create or update the turn's plan (the
structured task list). It is the planner's *output channel*: the
elected planner pass (task 8) nudges the model to call it for
multi-step work, and the executor pass re-injects the resulting plan.

The tool does two things, deliberately:

1. **Writes to the :class:`~gateway.plan_store.PlanStore`** for the
   session — durable state outside the model's working context
   (Story 1.2).
2. **Returns the ``{"todos": [...]}`` payload as its result** — so the
   plan also lands in conversation history and
   :func:`gateway.plan_store.derive_plan_from_history` can recover it
   if the in-memory store is cold (fresh agent per turn). Belt and
   braces.

Each call replaces the whole plan (matching OpenCode/Anthropic
``TodoWrite`` semantics: pass the full list every time). Bucket is
``auto`` — maintaining a task list is internal bookkeeping, not a
side-effecting action that warrants an approval prompt.
"""

from __future__ import annotations

import json
from typing import Any

from ..plan_store import PLAN_STATUSES, Plan
from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

_SCHEMA_TODOWRITE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": (
                "The full task list — REQUIRED. Pass the entire plan "
                "every call; it replaces the current one. Keep statuses "
                "current as you work (mark a step 'done' as soon as it "
                "is)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "What this step does (concrete, tool-oriented).",
                    },
                    "status": {
                        "type": "string",
                        "enum": list(PLAN_STATUSES),
                        "description": "Step status; defaults to 'pending'.",
                    },
                    "id": {
                        "type": "string",
                        "description": "Stable id; omit to auto-number by position.",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["todos"],
    "additionalProperties": False,
}


async def _tool_todowrite(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = ctx.plan_store
    if store is None:
        return ToolResult.error("plan store not available on this gateway")

    raw = args.get("todos")
    if not isinstance(raw, list):
        return ToolResult.error("'todos' is required and must be a list")

    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return ToolResult.error(f"todos[{i}] must be an object with a 'text' field")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult.error(f"todos[{i}].text is required and must be non-empty")
        normalized.append(
            {
                "id": str(item.get("id") or (i + 1)),
                "text": text.strip(),
                "status": item.get("status", "pending"),
            }
        )

    try:
        plan = Plan.from_dict({"todos": normalized})
    except ValueError as e:
        return ToolResult.error(str(e))

    store.set(ctx.session_key, plan)
    # Result content IS the todos payload so history-hydration can
    # recover the plan later; the model also sees its plan echoed.
    return ToolResult.ok(json.dumps(plan.to_dict()))


def build_plan_tools() -> list[Tool]:
    """Return the Phase 12 plan tools. Today just ``todowrite``;
    reading the plan is done by re-injection, not a tool."""
    return [
        Tool(
            name="todowrite",
            description=(
                "Create or update the structured task list (plan) for "
                "this turn. REQUIRED arg `todos`: the full ordered list "
                "of steps (pass the whole list each call; it replaces "
                "the current plan). Use it for multi-step work to lay "
                "out and track progress; mark steps done as you finish "
                "them."
            ),
            schema=_SCHEMA_TODOWRITE,
            callable=_tool_todowrite,
            default_bucket=ApprovalBucket.AUTO,
            kind="inline",
        ),
    ]

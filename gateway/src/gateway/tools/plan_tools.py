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


_STATUS_ALIASES: dict[str, str] = {
    "in progress": "in_progress",
    "in-progress": "in_progress",
    "inprogress": "in_progress",
    "active": "in_progress",
    "started": "in_progress",
    "doing": "in_progress",
    "complete": "done",
    "completed": "done",
    "finished": "done",
    "closed": "done",
    "todo": "pending",
    "open": "pending",
    "not_started": "pending",
    "waiting": "blocked",
    "stuck": "blocked",
}
"""Near-misses for :data:`PLAN_STATUSES`, mapped rather than rejected.

The status is metadata; the step text is the payload. Failing a whole
plan write because a model wrote "completed" instead of "done" trades a
usable plan for a pedantic error."""


def _coerce_status(raw: Any) -> str:
    """Best-effort map to a valid :data:`PlanStatus`; ``pending`` if unsure."""
    key = str(raw).strip().lower().replace("-", "_") if raw is not None else ""
    if key in PLAN_STATUSES:
        return key
    return _STATUS_ALIASES.get(key) or _STATUS_ALIASES.get(key.replace("_", " "), "pending")


async def _tool_todowrite(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    store = ctx.plan_store
    if store is None:
        return ToolResult.error("plan store not available on this gateway")

    raw = args.get("todos")
    if not isinstance(raw, list):
        return ToolResult.error("'todos' is required and must be a list")

    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        # A bare string is the shape models actually emit for a task
        # list, and rejecting it was killing turns rather than teaching
        # anything. Measured 2026-08-14: hermes3:8b elected to plan in 9
        # of 15 scenarios and sent `{"todos": ["step one", "step two"]}`
        # every time; the error came back "todos[0] must be an object
        # with a 'text' field", and on one scenario the failed plan call
        # was the turn's ONLY tool call and the user got an empty reply.
        # The plan tool is the planner's sole output channel, so a fumble
        # here costs the whole turn. Coerce; the intent is unambiguous.
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict):
            return ToolResult.error(
                f"todos[{i}] must be a step: either a plain string, or an "
                f'object like {{"text": "what the step does"}} — got '
                f"{type(item).__name__}"
            )
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult.error(
                f'todos[{i}].text is required and must be non-empty, e.g. {{"text": '
                f'"fetch today\'s news"}}'
            )
        # An invented status is a slip, not an instruction to abandon the
        # plan. Normalise rather than erroring: the step text is the
        # payload, and "in-progress" vs "in_progress" must not cost a turn.
        status = item.get("status", "pending")
        if status not in PLAN_STATUSES:
            status = _coerce_status(status)
        normalized.append(
            {
                "id": str(item.get("id") or (i + 1)),
                "text": text.strip(),
                "status": status,
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

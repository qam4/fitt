"""Phase 12 task 10 — the orchestrator.

Sequences a single turn as **plan -> execute** by driving the two
role-switched passes (:func:`gateway.planner.run_planner_pass` then
:func:`gateway.executor.run_executor_pass`) and aggregating their
result. It is a state machine, not an agent: it contains no model
intelligence (design D1) — the intelligence lives in the passes; the
orchestrator just owns the sequence and the PlanStore lifecycle for
the turn.

Recovery (task 13/14) and turn-event emission (task 17) are deliberate
later additions; this version is the bare plan -> execute spine so the
core hypothesis (elected planning helps a weak model on a multi-step
turn) becomes testable after task 12c without the heavier pieces.

**Not yet wired into chat/cron.** Making this the single entry point
the chat handler and cron runner call (replacing their direct
``run_agent_loop`` calls) is a deliberate prod-behaviour cutover: it
flips every turn to plan-then-execute and must preserve the assembled
system prompt (identity + capability block) that chat.py builds
upstream. That wiring — likely behind a config flag — is the
remaining part of task 10 and is intentionally left for an explicit
follow-up rather than flipped here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .executor import ExecutorResult, run_executor_pass
from .plan_store import Plan
from .planner import PlannerResult, run_planner_pass
from .prompt_resolver import PromptResolver
from .router import AliasRouter
from .tools import ToolContext, ToolRegistry

# Per-pass iteration caps. Planning should be tight (emit a plan,
# maybe revise once); execution gets the larger budget. Task 11
# replaces these literals with per-alias config (higher default for
# planned turns); until then they mirror the primitives' own
# defaults so behaviour is unchanged.
_DEFAULT_PLANNER_ITERATIONS = 3
_DEFAULT_EXECUTOR_ITERATIONS = 10


@dataclass(slots=True)
class OrchestratorResult:
    """Outcome of one orchestrated turn.

    * ``assistant_text`` — the executor's final reply (what the caller
      delivers to the user).
    * ``status`` — the executor loop's status (``ok`` /
      ``tool_loop_exhausted`` / ``upstream_error``).
    * ``planned`` — True iff the planner elected to emit a plan.
    * ``plan`` — the plan as it stands after execution (todo ticks
      applied), or ``None``.
    * ``plan_complete`` — True iff a plan exists and every item is done.
    * ``planner`` / ``executor`` — the underlying pass results for
      callers needing detail.
    * ``in_tokens`` / ``out_tokens`` — summed across both passes.
    """

    assistant_text: str
    status: str
    planned: bool
    plan: Plan | None
    plan_complete: bool
    planner: PlannerResult
    executor: ExecutorResult
    in_tokens: int
    out_tokens: int


async def run_orchestrated_turn(
    *,
    alias: str,
    user_message: str,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    prompt_resolver: PromptResolver,
    session_key: str,
    planner_max_iterations: int = _DEFAULT_PLANNER_ITERATIONS,
    executor_max_iterations: int = _DEFAULT_EXECUTOR_ITERATIONS,
) -> OrchestratorResult:
    """Run one turn as plan -> execute.

    The planner pass (elected) may write a plan to
    ``tool_ctx.plan_store``; the executor pass then runs with the full
    toolset and that plan re-injected (property C1). The planner pass
    runs every turn — election is the model's call inside it, not a
    branch here — so there is no separate fast path (a deliberate
    design choice; latency optimisation is a non-goal).
    """
    planner = await run_planner_pass(
        alias=alias,
        user_message=user_message,
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        prompt_resolver=prompt_resolver,
        session_key=session_key,
        max_iterations=planner_max_iterations,
    )

    executor = await run_executor_pass(
        alias=alias,
        user_message=user_message,
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        prompt_resolver=prompt_resolver,
        session_key=session_key,
        max_iterations=executor_max_iterations,
    )

    return OrchestratorResult(
        assistant_text=executor.loop.assistant_text,
        status=executor.loop.status,
        planned=planner.planned,
        plan=executor.plan,
        plan_complete=executor.plan_complete,
        planner=planner,
        executor=executor,
        in_tokens=planner.loop.in_tokens + executor.loop.in_tokens,
        out_tokens=planner.loop.out_tokens + executor.loop.out_tokens,
    )


__all__ = ["OrchestratorResult", "run_orchestrated_turn"]

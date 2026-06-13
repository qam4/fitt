"""Phase 12 task 10 — the orchestrator.

Runs one turn as **plan -> execute** and is shaped as a drop-in for
:func:`gateway.agent_loop.run_agent_loop`: same call surface (alias,
messages, request_body_extras, collaborators) and the same
:class:`AgentLoopResult` return, so the chat handler and cron runner
can route a turn through it without changing any downstream handling
(memory append, envelope shaping, detached delivery, cost accounting).

It is a state machine, not an agent (design D1) — the intelligence
lives in the two passes; the orchestrator owns the sequence:

1. **Plan pass** (lean, ``todowrite``-only) via
   :func:`gateway.planner.run_planner_pass` on the latest user turn.
   Elected: the model decides whether to emit a plan.
2. **Execute pass** via ``run_agent_loop`` on the *full assembled
   messages* (identity + capability block preserved), with the
   execute-step prompt and the plan re-injected into the system
   message (property C1), offering the original toolset.

The returned ``AgentLoopResult`` is the execute pass's, with the
planner pass's token and iteration counts folded in so cost
accounting reflects both passes.

Recovery (task 14) is wired in after the execute pass: a ground-truth
trouble signal (:mod:`gateway.trouble`) drives an escalating, bounded
recovery ladder (:mod:`gateway.recover`) — continue-nudge -> clean-
context re-plan -> honest stop — always on the same alias (never a
cloud escalation, property C6). A capability-gap reply ("I'd need a
tool to X") is a terminal honest outcome (task 15, Story 4.4) and is
delivered as-is, never recovered over. Turn-event emission (task 17)
is a deliberate later addition. Whether a turn is routed here at all is
gated per alias by ``Config.is_orchestrated`` (default off) — see the
chat/cron wiring.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from .agent_loop import AgentLoopResult, run_agent_loop
from .capabilities import parse_gap
from .plan_store import Plan
from .planner import run_planner_pass
from .prompt_resolver import PromptResolver
from .recover import decide_recovery, honest_report
from .router import AliasRouter
from .tools import ToolContext, ToolRegistry
from .trouble import detect_trouble
from .turn_events import (
    record_plan_created,
    record_plan_step_completed,
    record_plan_step_started,
    record_replan,
)

# Per-pass iteration-budget defaults, overridable per alias via
# AliasOrchestrationConfig (task 11). Planner defaults to 1: the plan
# is captured on the first `todowrite` call, so one model request
# suffices and keeps a cloud planner_alias under RPM limits (a second
# pass would only produce a reply we discard). Executor defaults
# higher than the flat loop's 10 — a planned turn works a multi-step
# plan and needs more tool round-trips (Story 3.3).
_DEFAULT_PLANNER_ITERATIONS = 1
_DEFAULT_EXECUTOR_ITERATIONS = 15


def _latest_user_message(messages: list[dict[str, Any]]) -> str:
    """The most recent user-role text in the assembled messages.

    The plan pass is deliberately lean — it plans against the user's
    actual request, not the whole transcript (matching the focused
    planner prompt). Returns ``""`` if there's no user message (the
    planner then simply elects not to plan)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
    return ""


def _plan_block(plan: Plan) -> str:
    return (
        "[Plan]\n"
        + plan.render_markdown()
        + "\n\nWork this plan: do the next step that is not yet done, and call "
        "`todowrite` to mark each step done as you complete it. When every step "
        "is done, give your final answer."
    )


def _augment_system(messages: list[dict[str, Any]], additions: list[str]) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with ``additions`` merged into the
    system message (appended to an existing leading system message, or
    prepended as one if absent). Preserves the assembled identity /
    capability prompt — we add to it, never replace it."""
    extra = "\n\n".join(a for a in additions if a and a.strip())
    if not extra:
        return list(messages)
    out = [dict(m) for m in messages]
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0] = {**out[0], "content": out[0]["content"] + "\n\n" + extra}
    else:
        out.insert(0, {"role": "system", "content": extra})
    return out


def _status_map(plan: Plan | None) -> dict[str, str]:
    """Snapshot ``{item_id: status}`` for plan-progress diffing."""
    if plan is None:
        return {}
    return {item.id: item.status for item in plan.items}


def _plan_items_meta(plan: Plan) -> list[dict[str, str]]:
    """Render a plan as the ``plan_created`` event's ``items`` payload."""
    return [{"id": i.id, "text": i.text, "status": i.status} for i in plan.items]


def _emit_step_transitions(
    turns: Any,
    turn_id: str | None,
    session_key: str,
    *,
    before: dict[str, str],
    after: Plan | None,
) -> None:
    """Emit a step-started / step-completed event for each plan item
    whose status advanced between ``before`` and ``after`` (Story 6.2).

    A pass can tick several todos (or skip in-progress straight to
    done); we diff the net status per item rather than tracking each
    ``todowrite`` call, which keeps the orchestrator decoupled from the
    tool internals while still surfacing progress."""
    if after is None:
        return
    for item in after.items:
        if before.get(item.id) == item.status:
            continue
        if item.status == "in_progress":
            record_plan_step_started(turns, turn_id, session_key, step_id=item.id, text=item.text)
        elif item.status == "done":
            record_plan_step_completed(turns, turn_id, session_key, step_id=item.id, text=item.text)


async def run_orchestrated_turn(
    *,
    alias: str,
    messages: list[dict[str, Any]],
    request_body_extras: dict[str, Any] | None = None,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    prompt_resolver: PromptResolver,
    session_key: str,
    planner_alias: str = "",
    planner_max_iterations: int | None = None,
    executor_max_iterations: int | None = None,
    artifact_store: Any = None,
) -> AgentLoopResult:
    """Run one turn as plan -> execute and return the execute pass's
    :class:`AgentLoopResult` (with the planner pass's tokens folded in).

    The planner pass runs every turn — election is the model's call
    inside it, not a branch here — so there is no separate fast path
    (a deliberate design choice; latency optimisation is a non-goal).

    ``planner_alias`` (when non-empty) runs the planner pass on a
    different alias than the executor — "plan with a capable model,
    execute with a fast one" (Story 2.2). Per-pass budgets default to
    ``_DEFAULT_*`` when ``None`` (task 11 / Story 3.3).
    """
    planner_budget = planner_max_iterations or _DEFAULT_PLANNER_ITERATIONS
    executor_budget = executor_max_iterations or _DEFAULT_EXECUTOR_ITERATIONS

    # Turn-event stream (Phase 7) handles, read off the ToolContext the
    # way the agent loop does. None-safe: no TurnLog wired -> no-op.
    turns = getattr(tool_ctx, "turns", None)
    turn_id = getattr(tool_ctx, "turn_id", None)

    planner = await run_planner_pass(
        alias=planner_alias or alias,
        user_message=_latest_user_message(messages),
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        prompt_resolver=prompt_resolver,
        session_key=session_key,
        max_iterations=planner_budget,
    )

    plan: Plan | None = None
    if tool_ctx.plan_store is not None:
        plan = tool_ctx.plan_store.get(session_key)

    # Story 6.1: surface the plan on the turn-event stream the moment
    # it's elected. An elected-out turn (no plan) emits nothing.
    if plan is not None and plan.items:
        record_plan_created(turns, turn_id, session_key, items=_plan_items_meta(plan))

    additions: list[str] = []
    execute_prompt = prompt_resolver.resolve("execute", alias)
    if execute_prompt.strip():
        additions.append(execute_prompt.strip())
    if plan is not None and plan.items:
        additions.append(_plan_block(plan))

    # Snapshot plan statuses so each pass's progress can be diffed into
    # step-started / step-completed events (Story 6.2).
    step_status = _status_map(plan)

    exec_messages = _augment_system(messages, additions)
    result = await run_agent_loop(
        alias=alias,
        messages=exec_messages,
        request_body_extras=request_body_extras,
        alias_router=alias_router,
        tool_registry=tool_registry,
        approval=approval,
        tool_ctx=tool_ctx,
        session_key=session_key,
        max_iterations=executor_budget,
        artifact_store=artifact_store,
    )
    plan_after = tool_ctx.plan_store.get(session_key) if tool_ctx.plan_store is not None else plan
    _emit_step_transitions(turns, turn_id, session_key, before=step_status, after=plan_after)
    step_status = _status_map(plan_after)

    # Token/iteration totals accumulate across the planner pass, the
    # executor pass, and any recovery re-runs so cost accounting
    # upstream sees every model request this turn made.
    total_in = result.in_tokens + planner.loop.in_tokens
    total_out = result.out_tokens + planner.loop.out_tokens
    total_iters = result.iterations + planner.loop.iterations

    # Recovery (task 14): if the executor pass hit a ground-truth
    # trouble signal, apply an escalating, bounded recovery action —
    # always on the SAME alias (never a cloud escalation, property
    # C6). The loop ends when the trouble clears, when recovery
    # decides to stop (honest report), or at the attempt cap.
    recover_prompt = prompt_resolver.resolve("recover", alias)
    attempt = 0
    replanned = False
    while True:
        # Capability gap is a TERMINAL honest outcome (Story 4.4),
        # distinct from thrash: the model has concluded it lacks a
        # tool to proceed ("I'd need a tool to X"). Deliver that reply
        # as-is and never nudge/replan over it — even if a trouble
        # signal (e.g. a preceding tool error) co-occurs, the gap is
        # the honest answer, not something to retry. The downstream
        # `_record_gap` still logs it to the capability-gap backlog.
        if parse_gap(result.assistant_text) is not None:
            break
        trouble = detect_trouble(
            status=result.status,
            tool_calls=result.tool_calls_for_memory,
            assistant_text=result.assistant_text,
        )
        if not trouble:
            break
        action = decide_recovery(trouble, attempt=attempt, replanned=replanned)
        if action == "stop":
            current_plan = (
                tool_ctx.plan_store.get(session_key) if tool_ctx.plan_store is not None else plan
            )
            # Deliver an honest report (Story 4.2) as the turn's reply.
            # status="ok" so the chat handler delivers the message
            # rather than returning an error envelope — the honest
            # "I couldn't do X" IS the successful outcome to surface.
            result = dataclasses.replace(
                result,
                status="ok",
                assistant_text=honest_report(trouble, current_plan),
            )
            break

        if action == "nudge":
            # Bounded retry on the EXISTING transcript: append the
            # recover-step prompt and re-run. Cheapest rung.
            run_messages = list(result.messages)
            if recover_prompt.strip():
                run_messages.append({"role": "system", "content": recover_prompt.strip()})
        else:  # replan — clean context (Story 5.3): discard the
            # flailing transcript, carry forward only the goal and the
            # progress-bearing plan re-injected from the store.
            replanned = True
            current_plan = (
                tool_ctx.plan_store.get(session_key) if tool_ctx.plan_store is not None else plan
            )
            record_replan(
                turns,
                turn_id,
                session_key,
                attempt=attempt,
                reason=f"{trouble.kind}: {trouble.detail}",
            )
            replan_additions: list[str] = []
            if recover_prompt.strip():
                replan_additions.append(recover_prompt.strip())
            elif execute_prompt.strip():
                replan_additions.append(execute_prompt.strip())
            if current_plan is not None and current_plan.items:
                replan_additions.append(_plan_block(current_plan))
            run_messages = _augment_system(messages, replan_additions)

        result = await run_agent_loop(
            alias=alias,
            messages=run_messages,
            request_body_extras=request_body_extras,
            alias_router=alias_router,
            tool_registry=tool_registry,
            approval=approval,
            tool_ctx=tool_ctx,
            session_key=session_key,
            max_iterations=executor_budget,
            artifact_store=artifact_store,
        )
        plan_after = (
            tool_ctx.plan_store.get(session_key) if tool_ctx.plan_store is not None else plan
        )
        _emit_step_transitions(turns, turn_id, session_key, before=step_status, after=plan_after)
        step_status = _status_map(plan_after)
        total_in += result.in_tokens
        total_out += result.out_tokens
        total_iters += result.iterations
        attempt += 1

    return dataclasses.replace(
        result,
        in_tokens=total_in,
        out_tokens=total_out,
        iterations=total_iters,
    )


__all__ = ["run_orchestrated_turn"]

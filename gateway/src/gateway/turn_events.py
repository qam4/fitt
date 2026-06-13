"""Phase 4.8 turn-event emission helpers.

Thin wrappers over :meth:`gateway.turns.TurnLog.append` that
package each call site's kind-specific fields into a
:class:`~gateway.turns.TurnEvent` with the correct ``meta``
shape. Callers in :mod:`gateway.agent_loop`, :mod:`gateway.chat`,
:mod:`gateway.cron_runner`, and :mod:`gateway.approval` import
these helpers rather than building events by hand — one source
of truth for the per-kind schema documented in
``.kiro/specs/phase4.8-visibility-proxies/design.md``.

Every helper

- Accepts ``turns: TurnLog | None`` as the first arg. ``None``
  is a no-op. Matches the posture of ``record_gap`` /
  ``record_narrated_tool_call``-before-removal: observability
  is optional, never load-bearing.
- Accepts ``turn_id: str | None``. ``None`` short-circuits.
  A caller that couldn't generate a turn id (early-phase
  tests, legacy call sites) simply skips turn-event
  emission without crashing.
- Swallows any exception from :meth:`TurnLog.append` and
  logs at WARNING. The agent loop must never die because a
  turn-event write failed.

Kept as free functions, not methods on some ``TurnEventEmitter``
class, to keep the call sites one-liners:

    record_turn_started(turns, turn_id, session_key,
                        alias=alias, client=client,
                        user_msg_len=len(user_msg))

No class hierarchy, no state beyond the already-stateful
``TurnLog``. Signatures are kwarg-only after the positional
(turns, turn_id, session_key) triple so a future schema
tweak (add a ``meta`` field) doesn't silently land in the
wrong slot at a call site.

Relationship to :func:`gateway.agent_loop.record_gap`
------------------------------------------------------

``record_gap`` in ``agent_loop`` writes to
``$FITT_HOME/capability_gaps.log`` (the ranked "I'd need a
tool to X" backlog). :func:`record_gap_event` here writes
a ``gap_reported`` turn event for the per-turn stream.
Different logs, different lifespans, same underlying
detection — the two stay in sync by both being called from
``agent_loop`` with the same inputs.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

from .turns import new_event

if TYPE_CHECKING:
    from .turns import TurnLog

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- internal


def _emit(
    turns: TurnLog | None,
    *,
    turn_id: str | None,
    kind: str,
    session_key: str,
    meta: dict[str, Any],
) -> None:
    """Construct a turn event and append it, swallowing errors.

    Single funnel for every ``record_*`` helper so the "log
    on failure, never raise" posture lives in one place. A
    future schema tweak (e.g. adding a timestamp precision
    field) lands here once."""
    if turns is None or turn_id is None:
        return
    try:
        turns.append(
            new_event(
                turn_id=turn_id,
                kind=kind,
                session_key=session_key,
                meta=meta,
            )
        )
    except Exception as exc:
        _log.warning(
            "turn_events.emit_failed",
            extra={
                "kind": kind,
                "session_key": session_key,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )


# --------------------------------------------------------------- turn lifecycle


def record_turn_started(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    alias: str,
    client: str,
    user_msg_len: int,
) -> None:
    """A tool-using turn has begun.

    ``user_msg_len`` is a size indicator only (not the content
    itself) so scrubbed operators can see "the user typed ~200
    chars" in scrollback without the full message landing
    twice (once in history, once here)."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="turn_started",
        session_key=session_key,
        meta={
            "alias": alias,
            "client": client,
            "user_msg_len": user_msg_len,
        },
    )


def record_turn_finished(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    status: Literal["ok", "upstream_error", "tool_loop_exhausted"],
    iterations: int,
    final_reply_len: int,
) -> None:
    """Turn is done. ``status`` matches
    :class:`~gateway.agent_loop.AgentLoopResult.status`
    byte-for-byte so subscribers can branch on it without a
    translation layer."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="turn_finished",
        session_key=session_key,
        meta={
            "status": status,
            "iterations": iterations,
            "final_reply_len": final_reply_len,
        },
    )


# --------------------------------------------------------------- LLM dispatch


def record_llm_call_started(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    alias: str,
    iteration: int,
) -> None:
    """The agent loop is about to dispatch to the LLM. Paired
    with :func:`record_llm_call_completed` — the difference
    in their ``ts`` fields is the upstream latency."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="llm_call_started",
        session_key=session_key,
        meta={
            "alias": alias,
            "iteration": iteration,
        },
    )


def record_llm_call_completed(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    model: str,
    latency_ms: int,
    in_tokens: int,
    out_tokens: int,
    finish_reason: str | None,
    tool_calls_count: int,
    cost_usd: Decimal | None,
) -> None:
    """The LLM replied. ``model`` is the concrete backend model
    id (``deepseek-v4-flash``, ``qwen2.5-coder:14b``), not the
    FITT alias — alias is on the paired ``llm_call_started``.
    ``cost_usd`` is ``None`` when the backend is local or the
    model has no configured cost (local Ollama)."""
    meta: dict[str, Any] = {
        "model": model,
        "latency_ms": latency_ms,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "finish_reason": finish_reason,
        "tool_calls_count": tool_calls_count,
    }
    if cost_usd is not None:
        # Decimal isn't JSON-serialisable; convert to float at
        # the schema boundary. Precision loss is acceptable —
        # costs land in audit.jsonl with full precision; the
        # turn stream is for observation.
        meta["cost_usd"] = float(cost_usd)
    _emit(
        turns,
        turn_id=turn_id,
        kind="llm_call_completed",
        session_key=session_key,
        meta=meta,
    )


# --------------------------------------------------------------- tool calls


def record_tool_call_planned(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    tool_name: str,
    args: dict[str, Any],
    call_id: str,
    iteration: int,
) -> None:
    """The model emitted a ``tool_calls`` entry. ``args`` is
    the structured dict (not a summary) — per P8 the turn
    log captures what happened verbatim, including anything
    the model invented. Secrets that leak into args are a
    tool-layer problem, not a log-layer problem."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="tool_call_planned",
        session_key=session_key,
        meta={
            "tool_name": tool_name,
            "args": args,
            "call_id": call_id,
            "iteration": iteration,
        },
    )


def record_tool_call_executed(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    tool_name: str,
    call_id: str,
    ok: bool,
    duration_ms: int,
    result_summary: str,
    artifact_path: str | None = None,
    exit_code: int | None = None,
) -> None:
    """The tool finished.

    ``result_summary`` is capped at 300 chars by contract — a
    long result lands on disk as an artifact (see
    :mod:`gateway.tool_artifacts`) and ``artifact_path``
    points at it. ``exit_code`` is set for ``project_shell``
    invocations so operators can grep
    ``fitt watch --kind tool_call_executed | grep exit=1``.
    """
    meta: dict[str, Any] = {
        "tool_name": tool_name,
        "call_id": call_id,
        "ok": ok,
        "duration_ms": duration_ms,
        "result_summary": (
            result_summary if len(result_summary) <= 300 else result_summary[:297] + "..."
        ),
    }
    if artifact_path is not None:
        meta["artifact_path"] = artifact_path
    if exit_code is not None:
        meta["exit_code"] = exit_code
    _emit(
        turns,
        turn_id=turn_id,
        kind="tool_call_executed",
        session_key=session_key,
        meta=meta,
    )


# --------------------------------------------------------------- approval


def record_approval_requested(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    approval_id: str,
    tool_name: str,
    bucket: str,
    client: str,
) -> None:
    """An approval has been created and the agent loop is
    awaiting the user's decision. The renderer uses this
    event to post a notifying Telegram bubble with the
    inline keyboard."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="approval_requested",
        session_key=session_key,
        meta={
            "approval_id": approval_id,
            "tool_name": tool_name,
            "bucket": bucket,
            "client": client,
        },
    )


def record_approval_decided(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    approval_id: str,
    decision: Literal["approve", "reject", "trust_session", "timeout", "denied_deny_list"],
    duration_ms: int,
) -> None:
    """The approval resolved (user decided or the wait timed
    out). ``decision`` values match the
    :class:`~gateway.tools._types.ApprovalDecision.reason`
    literals, extended with ``"timeout"`` for the wait
    expiry branch. The renderer edits its approval bubble
    in place to show the outcome."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="approval_decided",
        session_key=session_key,
        meta={
            "approval_id": approval_id,
            "decision": decision,
            "duration_ms": duration_ms,
        },
    )


# --------------------------------------------------------------- gaps


def record_gap_event(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    gap_text: str,
    suggestion: str,
) -> None:
    """The model emitted an "I'd need a tool to X" gap report.

    Complements :func:`gateway.agent_loop.record_gap`, which
    writes to ``$FITT_HOME/capability_gaps.log`` (the ranked
    product backlog). This version lands in the per-turn
    stream so ``fitt watch`` and the live renderer surface
    the gap at the moment it was reported rather than as a
    background aggregation."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="gap_reported",
        session_key=session_key,
        meta={
            "gap_text": gap_text,
            "suggestion": suggestion,
        },
    )


# --------------------------------------------------------------- planning (Phase 12)


def record_plan_created(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    items: list[dict[str, str]],
) -> None:
    """The planner pass produced a plan (Phase 12, Story 6.1).

    ``items`` is the ordered plan as ``[{"id", "text", "status"}, ...]``
    — the full plan, so the live renderer and the dashboard turn-detail
    page can show it without re-reading the PlanStore. Emitted once per
    turn when planning is elected; an elected-out turn emits nothing."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="plan_created",
        session_key=session_key,
        meta={
            "item_count": len(items),
            "items": items,
        },
    )


def record_plan_step_started(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    step_id: str,
    text: str,
) -> None:
    """A plan step transitioned to in-progress (Story 6.2)."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="plan_step_started",
        session_key=session_key,
        meta={"step_id": step_id, "text": text},
    )


def record_plan_step_completed(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    step_id: str,
    text: str,
) -> None:
    """A plan step transitioned to done (Story 6.2)."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="plan_step_completed",
        session_key=session_key,
        meta={"step_id": step_id, "text": text},
    )


def record_replan(
    turns: TurnLog | None,
    turn_id: str | None,
    session_key: str,
    *,
    attempt: int,
    reason: str,
) -> None:
    """Recovery restarted execution on a clean context (Story 6.2).

    ``attempt`` is the recovery attempt index; ``reason`` is the
    ground-truth trouble kind/detail that triggered the re-plan — a
    fact, never an interpretation of prose (property C4)."""
    _emit(
        turns,
        turn_id=turn_id,
        kind="replan",
        session_key=session_key,
        meta={"attempt": attempt, "reason": reason},
    )

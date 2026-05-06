"""Phase 4.5 Task 5.5 — detached delivery for late-approved tools.

Closes the Phase 4 rough edge where a tool approved after the HTTP
client's timeout would run successfully but vanish — the result
was stuck in audit.jsonl with no visible trail to the user.

Shape
-----

The chat handler wraps its ``run_agent_loop`` call in
:func:`run_with_detach`. Normally the loop completes within the
detach threshold and the caller returns synchronously, same as
before. When the threshold expires while the loop is still stuck
on an ``ask`` prompt:

1. :func:`run_with_detach` cancels its wait and returns a
   ``DetachedPending`` sentinel.
2. The chat handler treats that as "placeholder path": returns
   the canned ``"⏳ Approval pending ..."`` response to the client.
3. Meanwhile the loop task runs on untouched — ``asyncio.shield``
   stops the ``wait_for`` cancellation from propagating.
4. When the loop finishes (user approves → tool runs → model
   stops, or user rejects → tool_error → model stops), the
   background worker emits a ``late_tool_result`` or
   ``late_tool_rejected`` event and appends the turn to memory.

The chat handler never sees the detached-task result directly; it
only awaits :func:`run_with_detach`, which either returns the
real :class:`AgentLoopResult` (happy path) or the
``DetachedPending`` sentinel (detach path). That keeps the HTTP
code small and the detached-worker logic isolated here.

No-push-channel fallback
------------------------

If nothing is polling the event log (no Telegram bot, no other
push subscriber), the late event still lands — it's just only
visible via ``fitt inbox``. Task 5.5d: log a clear WARNING at
detach time so the operator sees it during live testing.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .agent_loop import AgentLoopResult
from .events import new_entry as new_event

if TYPE_CHECKING:
    from .events import EventLog
    from .memory import MemoryStore

_log = logging.getLogger(__name__)


# Background detached workers live here so the event loop's weak
# reference to the task doesn't let Python garbage-collect the
# coroutine mid-run. See the RUF006 lint + cpython docs for
# ``asyncio.create_task`` — we add to this set when spawning,
# remove on completion via ``add_done_callback``.
_BACKGROUND_WORKERS: set[asyncio.Task[None]] = set()


PLACEHOLDER_MESSAGE = "⏳ Approval pending — I'll message you when this completes."
"""Synchronous response when the chat handler detaches.

Matches the wording in the Phase 4.5 design doc. Short enough to
fit a push notification preview, Unicode-safe for every IDE, and
uses the ⏳ glyph so the user can eyeball "pending, not errored"
at a glance."""


@dataclass(slots=True)
class DetachedPending:
    """Sentinel returned by :func:`run_with_detach` when the agent
    loop is still running past the detach threshold.

    Carries nothing — the handler doesn't need the task reference;
    the :func:`run_with_detach` call has already arranged for the
    background worker to handle the remainder. A dataclass
    rather than a singleton so the type checker can pattern-match
    on it (`isinstance(result, DetachedPending)`).
    """


async def run_with_detach(
    loop_coro_factory: Any,
    *,
    detach_threshold_s: float | None,
    on_detach: Any | None = None,
) -> AgentLoopResult | DetachedPending:
    """Run the agent loop with a detach threshold.

    Parameters
    ----------
    loop_coro_factory:
        Zero-arg callable returning the coroutine to await. Using
        a factory rather than a raw coroutine keeps the call site
        simple for callers that construct the coroutine inline
        and lets the helper build the task exactly once.
    detach_threshold_s:
        Seconds to wait before detaching. ``None`` (or a value
        greater than the loop's own internal timeout) means
        effectively no detach — wait as long as the loop needs
        and return the result synchronously.
    on_detach:
        Optional coroutine factory called once with the in-flight
        :class:`asyncio.Task` when the handler detaches. The
        caller wires this to a background worker that awaits the
        task and emits the late event. Not awaited here — the
        caller is responsible for scheduling it.

    Returns
    -------
    Either the real :class:`AgentLoopResult` (happy path, the
    loop finished within the threshold) or a :class:`DetachedPending`
    sentinel (the loop is still running and will complete in the
    background).
    """
    task: asyncio.Task[AgentLoopResult] = asyncio.create_task(loop_coro_factory())
    if detach_threshold_s is None:
        # No detach configured — just await the task. ``shield``
        # is unnecessary here because nothing is going to cancel
        # the wait from above: no wait_for timeout to trip.
        return await task
    try:
        # ``shield`` keeps the task alive even if our ``wait_for``
        # trips its timeout. The caller (background worker) still
        # holds ``task`` and can await it to completion.
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=detach_threshold_s,
        )
    except TimeoutError:
        _log.info(
            "chat.detached",
            extra={"threshold_s": detach_threshold_s},
        )
        if on_detach is not None:
            try:
                # Schedule the background worker; the caller
                # returns the placeholder response and lets this
                # task own the rest of the lifecycle. We hold a
                # strong reference on ``_BACKGROUND_WORKERS`` so
                # the coroutine isn't garbage-collected mid-run
                # (the event loop only holds a weak ref).
                worker = asyncio.create_task(on_detach(task), name="fitt-detached-worker")
                _BACKGROUND_WORKERS.add(worker)
                worker.add_done_callback(_BACKGROUND_WORKERS.discard)
            except Exception as exc:
                # If the worker can't even be scheduled, cancel
                # the loop task so we don't leak a coroutine.
                _log.warning(
                    "chat.detach.worker_schedule_failed",
                    extra={"error": str(exc)},
                )
                task.cancel()
                raise
        return DetachedPending()


async def finish_detached(
    task: asyncio.Task[AgentLoopResult],
    *,
    session_key: str,
    user_message: str,
    events: EventLog | None,
    memory: MemoryStore | None,
    tool_name: str = "",
    approval_id: str = "",
    original_client: str = "",
    push_channel_available: bool = True,
) -> None:
    """Background worker: await ``task``, emit the right event,
    append the completed turn to memory.

    Called by :func:`run_with_detach` via ``on_detach``. Swallows
    every exception — the task has already detached from the
    HTTP request and raising here would just log a "Task
    exception was never retrieved" warning with no user-visible
    impact.
    """
    try:
        result = await task
    except asyncio.CancelledError:
        # The only reason the task should get cancelled is
        # gateway shutdown. Log and move on; the docstring on
        # the design doc calls this a "documented limitation":
        # a gateway restart while a detached worker is running
        # loses the tool result.
        _log.info(
            "chat.detached.cancelled",
            extra={"session_key": session_key, "tool": tool_name},
        )
        return
    except Exception as exc:
        # The tool loop itself swallows tool errors into tool_result
        # messages, so an exception here means something more
        # fundamental (upstream unreachable, malformed response).
        # Surface as a late_tool_rejected with the error detail;
        # user sees "pending → failed" rather than silent drop.
        _log.warning(
            "chat.detached.failed",
            extra={
                "session_key": session_key,
                "tool": tool_name,
                "error": str(exc),
            },
        )
        _emit_late_event(
            events,
            kind="late_tool_rejected",
            session_key=session_key,
            title=f"⚠️ late failure: {tool_name or 'tool'}",
            body=f"{type(exc).__name__}: {exc}",
            meta={
                "tool": tool_name,
                "approval_id": approval_id,
                "original_session_key": session_key,
                "original_client": original_client,
                "reason": "upstream_error",
                "traceback": _tb_tail(exc),
            },
        )
        return

    # The loop completed. Pick the event kind based on whether
    # the final reply reflects a tool that got to run.
    #
    # The simplest, most robust signal we have at this layer is
    # the loop's status plus a quick scan of the last iteration's
    # tool-result messages. We could thread a richer "was every
    # tool rejected" flag through the agent loop, but that's a
    # bigger refactor for little win; the reply text is already
    # populated either way and Telegram-side formatting is
    # downstream.
    kind = "late_tool_result"
    if _final_tool_was_rejected(result):
        kind = "late_tool_rejected"

    assistant_text = result.assistant_text
    _emit_late_event(
        events,
        kind=kind,
        session_key=session_key,
        title=_title_for_late_event(kind, tool_name),
        body=assistant_text,
        meta={
            "tool": tool_name,
            "approval_id": approval_id,
            "original_session_key": session_key,
            "original_client": original_client,
            "status": result.status,
            "iterations": result.iterations,
        },
    )

    # Persist the turn so "what did we talk about earlier" keeps
    # working when the user scrolls back in Telegram. Same
    # invariant as the synchronous path.
    if memory is not None and user_message and assistant_text and result.status == "ok":
        try:
            memory.append_turn(session_key, user_message, assistant_text)
        except Exception as exc:
            _log.warning(
                "chat.detached.memory_append_failed",
                extra={
                    "session_key": session_key,
                    "error": str(exc),
                },
            )

    if not push_channel_available:
        _log.warning(
            "chat.detached.no_push_channel",
            extra={
                "session_key": session_key,
                "kind": kind,
                "tool": tool_name,
            },
        )


def _final_tool_was_rejected(result: AgentLoopResult) -> bool:
    """Best-effort check: did the user reject the approval prompt?

    We look at the most recent tool-role message in the loop's
    working message list. If it starts with the approval
    middleware's rejected/timeout detail, the path was "detached
    → user tapped reject / prompt expired". Otherwise the tool
    ran and we treat it as a result.

    This is intentionally a soft heuristic — the only cost of
    misclassification is a slightly off event title; the body
    (the model's final reply) tells the real story either way.
    """
    # Scan messages backwards for the last tool-result.
    for msg in reversed(result.messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            lowered = content.lower()
            if "rejected by user" in lowered:
                return True
            if "approval timed out" in lowered:
                return True
        break  # stop at the first (most recent) tool message
    return False


def _title_for_late_event(kind: str, tool_name: str) -> str:
    """Short title suitable for a push-notification preview."""
    tag = tool_name or "tool"
    if kind == "late_tool_rejected":
        return f"⚠️ late rejection: {tag}"
    return f"✅ late result: {tag}"


def _emit_late_event(
    events: EventLog | None,
    *,
    kind: str,
    session_key: str,
    title: str,
    body: str,
    meta: dict[str, Any],
) -> None:
    """Append the event, swallowing disk errors. Events are a
    notification feed; failing to write one shouldn't crash the
    background worker, it just means the user misses one late
    notification."""
    if events is None:
        return
    try:
        events.append(
            new_event(
                kind=kind,
                session_key=session_key,
                title=title,
                body=body,
                meta=meta,
            )
        )
    except Exception as exc:
        _log.warning(
            "chat.detached.event_emit_failed",
            extra={"error": str(exc), "kind": kind},
        )


def _tb_tail(exc: BaseException | None, limit: int = 800) -> str:
    """Last ~limit chars of a traceback string. Same helper as
    :mod:`gateway.cron_runner` uses for ``cron_failed`` meta; copied
    rather than imported to avoid an import cycle between
    ``chat``-adjacent modules."""
    if exc is None:
        return ""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return tb[-limit:]


def build_placeholder_response(
    body_template: dict[str, Any] | None = None,
    *,
    model: str,
) -> dict[str, Any]:
    """Construct the non-streaming OpenAI-shape body for a
    detached chat turn.

    Mirrors the shape the agent loop would produce on success so
    IDE clients can parse it with no special-casing. The ``id``
    and ``object`` fields come from :data:`body_template` when
    the caller has a partial response (unusual — most detach
    cases happen before a model reply lands), otherwise synthetic
    defaults."""
    template = body_template or {}
    return {
        "id": template.get("id", "chatcmpl-fitt-detached"),
        "object": template.get("object", "chat.completion"),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": PLACEHOLDER_MESSAGE,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }

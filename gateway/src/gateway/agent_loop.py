"""Headless agent loop — dispatch, tool-execute, repeat, return.

Extracted from :mod:`gateway.chat` during Phase 4.5 so cron
firings (and, later, the Phase 6 spec runner) can drive the same
machinery without an HTTP request in flight. The chat endpoint
now wraps this function with its stream/non-stream envelope
shaping and request logging; the cron fire callback calls it
directly with a synthesized ``messages`` list.

What "headless" means here:

* No ``Request`` / ``Response`` types; pure async function.
* Takes the alias router, tool registry, approval middleware,
  memory store, and a pre-built :class:`ToolContext`. The caller
  owns wiring — this module owns the loop.
* Returns a :class:`AgentLoopResult` with everything the caller
  needs to build a response or emit an event: the final assistant
  text, the raw response dict from the upstream model, the full
  message list (for memory replay or detached-delivery
  continuation), token counts, and a ``status`` discriminant.
* Bounded by ``_MAX_TOOL_CALL_ITERATIONS`` (10). Going over emits
  ``status="tool_loop_exhausted"`` so the caller can surface the
  right HTTP code (504 for chat) or event kind (``cron_failed``
  for cron).

What this module *doesn't* do:

* HTTP status translation — live in :mod:`chat`.
* Streaming-envelope rewriting — live in :mod:`chat`.
* Cost computation — trivial on the caller side via
  :func:`gateway.cost.estimate_cost` + ``model_used`` + token
  counts.
* Memory persistence — caller owns it. We *do* read the
  capability-gap log because parsing the final assistant text
  for gaps is a loop-level concern (we need the last reply in
  hand); the caller passes in the log it wants us to write to.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .audit import new_entry as new_audit_entry
from .capabilities import detect_narrated_tool_call, is_tool_use_expected_but_none, parse_gap
from .errors import NoBackendAvailable, UnknownAlias, UnknownTool
from .memory import PersistedToolCall
from .router import AliasRouter
from .tools import ApprovalDecision, Tool, ToolContext, ToolRegistry, ToolResult

_log = logging.getLogger(__name__)


# Safety rail on tool-call loops. The model could in principle
# call tools forever; bound it. Picked by feel — enough for a
# multi-step "read a few files, grep, summarize" turn without
# giving a runaway loop room to fester.
_MAX_TOOL_CALL_ITERATIONS = 10


# --------------------------------------------------------------- result


@dataclass(slots=True)
class AgentLoopResult:
    """What the loop produces. ``status`` is the discriminant:

    * ``"ok"`` — model produced a final assistant reply; consumer
      should deliver ``assistant_text`` (and usually append to
      memory).
    * ``"tool_loop_exhausted"`` — hit the iteration cap; consumer
      should surface as an error to the user and probably log.
    * ``"upstream_error"`` — the upstream model dispatch raised;
      ``error`` carries the exception for translation.

    ``response_obj``, ``model_used``, ``fallback_used``, and
    ``messages`` are useful regardless of status (a late
    ``upstream_error`` still has partial data)."""

    status: str
    assistant_text: str = ""
    response_obj: Any = None
    model_used: Any = None
    fallback_used: bool = False
    in_tokens: int = 0
    out_tokens: int = 0
    iterations: int = 0
    error: Exception | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls_for_memory: list[PersistedToolCall] = field(default_factory=list)
    """Phase 5 — structured record of every tool call this turn
    executed. Populated by :func:`run_agent_loop` regardless of
    ``status`` (a failed turn still ran tools that the caller
    might want to persist). Consumers pass this list to
    :meth:`MemoryStore.append_turn(tool_calls=...)` so future
    context sees the OUTCOME of past tool calls, not just the
    paraphrased reply."""


# --------------------------------------------------------------- response shape helpers


def response_to_dict(response: Any) -> dict[str, Any] | None:
    """Coerce a LiteLLM response (pydantic or dict) to a dict."""
    if response is None:
        return None
    if hasattr(response, "model_dump"):
        dumped = response.model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else None
    if isinstance(response, dict):
        return response
    return None


def extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Return the model's ``tool_calls`` array from a response, or
    ``[]`` if no tools were requested. Handles the subtle case
    where finish_reason is ``"stop"`` but tool_calls is present
    anyway — treat as "done", ignore the dangling calls."""
    dumped = response_to_dict(response)
    if dumped is None:
        return []
    choices = dumped.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    choice0 = choices[0]
    if not isinstance(choice0, dict):
        return []
    finish_reason = choice0.get("finish_reason")
    msg = choice0.get("message")
    if not isinstance(msg, dict):
        return []
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return []
    if finish_reason not in (None, "tool_calls"):
        return []
    return [tc for tc in tool_calls if isinstance(tc, dict)]


def assistant_message_from_response(response: Any) -> dict[str, Any] | None:
    dumped = response_to_dict(response)
    if dumped is None:
        return None
    choices = dumped.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice0 = choices[0]
    if not isinstance(choice0, dict):
        return None
    msg = choice0.get("message")
    return msg if isinstance(msg, dict) else None


def extract_assistant_text(response: Any) -> str:
    """Pull the assistant's text from a non-streaming response."""
    dumped = response_to_dict(response)
    if not dumped:
        return ""
    choices = dumped.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    return content if isinstance(content, str) else ""


def extract_usage(response: Any) -> tuple[int, int]:
    if response is None:
        return 0, 0
    usage = getattr(response, "usage", None) or (
        response.get("usage") if isinstance(response, dict) else None
    )
    if not usage:
        return 0, 0
    if hasattr(usage, "prompt_tokens"):
        return int(usage.prompt_tokens or 0), int(usage.completion_tokens or 0)
    if isinstance(usage, dict):
        return (
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )
    return 0, 0


def tool_call_args(call: dict[str, Any]) -> dict[str, Any]:
    """Parse the args dict out of an OpenAI tool_call payload.
    Best-effort — the loop itself tolerates malformed args."""
    fn = call.get("function") or {}
    raw = fn.get("arguments") if isinstance(fn, dict) else None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


# --------------------------------------------------------------- tool execution


async def execute_tool_call(
    call: dict[str, Any],
    *,
    registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
) -> tuple[str, ToolResult, ApprovalDecision, Tool | None]:
    """Resolve one tool call. Any failure surface — unknown tool,
    bad args, approval reject — is expressed as a ToolResult with
    is_error=True so the loop can continue."""
    call_id = str(call.get("id") or "")
    function = call.get("function") or {}
    name = function.get("name") if isinstance(function, dict) else None
    raw_args = function.get("arguments") if isinstance(function, dict) else None

    if not isinstance(name, str) or not name:
        return (
            call_id,
            ToolResult.error("tool_call missing function.name"),
            ApprovalDecision.rejected(detail="malformed tool_call"),
            None,
        )

    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as e:
            return (
                call_id,
                ToolResult.error(f"tool {name!r} arguments are not valid JSON: {e}"),
                ApprovalDecision.rejected(detail="bad args"),
                None,
            )
        if not isinstance(args, dict):
            return (
                call_id,
                ToolResult.error(f"tool {name!r} arguments must be a JSON object"),
                ApprovalDecision.rejected(detail="bad args"),
                None,
            )
    else:
        args = {}

    try:
        tool = registry.lookup(name)
    except UnknownTool:
        return (
            call_id,
            ToolResult.error(
                f"tool {name!r} is not registered; this is likely a "
                f"hallucinated call. Try again using only the tools "
                f"listed in the capabilities block."
            ),
            ApprovalDecision.rejected(detail="unknown tool"),
            None,
        )

    decision = await approval.check(tool, args, tool_ctx)
    if not decision.execute:
        return (
            call_id,
            ToolResult.error(decision.detail or f"tool {name!r} not executed"),
            decision,
            tool,
        )

    try:
        result = await tool.callable(args, tool_ctx)
    except Exception as exc:
        _log.warning(
            "tool.execute_failed",
            extra={"tool": name, "error": str(exc)},
        )
        result = ToolResult.error(f"tool {name!r} raised {type(exc).__name__}: {exc}")
    return call_id, result, decision, tool


# --------------------------------------------------------------- capability gaps


def record_gap(
    gap_log: Any,
    assistant_text: str,
    session_key: str,
    *,
    tool_registry: Any = None,
) -> None:
    """Append a capability-gap entry if the assistant's final
    reply contains the standard gap phrasing. Swallowed errors
    keep gap logging from ever breaking a successful turn.

    When ``tool_registry`` is provided and the parsed gap's
    ``suggestion`` matches a tool that's already registered,
    skip the append and log ``capabilities.gap_false_positive``
    instead. This catches the 2026-05-10 failure mode where a
    different bug (argument-name errors) made the model fall
    back to the gap-reporter phrasing for tools it actually
    had, polluting the log with entries like "I'd need a tool
    to read a file. Consider adding read_file." for a
    read_file that was registered the entire time.

    The filter is suggestion-only. A short-form gap
    ("I'd need a tool to X." with no "Consider adding")
    logs as before — we can't tell which tool it means.
    That's intentional: false-negative (missing some noise
    entries) beats false-positive (suppressing real gap
    reports that just happened not to include a suggestion)."""
    if not assistant_text or gap_log is None:
        return
    try:
        gap = parse_gap(assistant_text, session_key=session_key)
    except Exception as exc:
        _log.debug("capabilities.parse_gap_failed", extra={"error": str(exc)})
        return
    if gap is None:
        return
    if tool_registry is not None and gap.suggestion:
        # Suggestion text is often prose ("Consider adding
        # ``read_file``" or "consider a read_file tool"). Strip
        # punctuation/backticks and check each word against
        # the registry — if any word matches a registered tool
        # name, this is a false positive.
        suggestion_tokens = {
            token.strip("`'\".,;:!?()[]{}").lower() for token in gap.suggestion.split()
        }
        try:
            registered_names = {n.lower() for n in tool_registry.list_names()}
        except Exception as exc:
            _log.debug(
                "capabilities.registry_check_failed",
                extra={"error": str(exc)},
            )
            registered_names = set()
        collision = suggestion_tokens & registered_names
        if collision:
            _log.info(
                "capabilities.gap_false_positive",
                extra={
                    "session_key": session_key,
                    "action": gap.action,
                    "suggestion": gap.suggestion,
                    "matched_tool": sorted(collision)[0],
                    "hint": (
                        "model used gap-reporter phrasing for a "
                        "tool that is already registered; not "
                        "appending to capability_gaps.log"
                    ),
                },
            )
            return
    try:
        gap_log.append(gap)
    except Exception as exc:
        _log.warning("capabilities.gap_append_failed", extra={"error": str(exc)})


# --------------------------------------------------------------- narrated tool calls


def record_narrated_tool_call(
    events: Any,
    assistant_text: str,
    *,
    session_key: str,
    alias: str,
    iterations: int,
    tools_were_offered: bool,
    finish_reason: str | None = "stop",
    had_real_tool_calls: bool = False,
) -> None:
    """Emit a ``tool_call_narrated`` event when the turn LOOKED
    like it should have produced a tool call but didn't.

    Uses :func:`capabilities.is_tool_use_expected_but_none` as
    the decision gate — model-independent shape check, no
    regex on specific narration patterns. Catches JSON-fence
    narration, TOOL_NAME: sentinel narration, capability false-
    negatives ("I can't do that" when the tool is listed),
    stubborn-training-data replies, and anything next month's
    model invents in place of real ``tool_calls``.

    The event kind stays ``tool_call_narrated`` for back-compat
    with ``fitt inbox`` filters and existing consumers, even
    though the semantic is now broader than JSON-fence narration.

    As a best-effort niceness: if the reply happens to contain
    a JSON-fenced tool-call-shaped payload, extract the tool
    name + fence body for the event's title and body so the
    operator gets a concrete preview. When no fence matches,
    fall back to a generic title and a truncated reply snippet.

    Swallows all errors — observability should never break the
    turn that succeeded modulo the narration.
    """
    if events is None:
        return
    try:
        should_emit = is_tool_use_expected_but_none(
            assistant_text,
            tools_were_offered=tools_were_offered,
            finish_reason=finish_reason,
            had_real_tool_calls=had_real_tool_calls,
        )
    except Exception as exc:
        _log.debug(
            "capabilities.shape_check_failed",
            extra={"error": str(exc)},
        )
        return
    if not should_emit:
        return
    # Best-effort regex match to pick out a concrete tool name
    # and fence body for the event. Falls back to a generic
    # title when no fence matches (which is the whole point of
    # the new shape check: catch narration regardless of shape).
    narrated_name = ""
    event_body = assistant_text[:500]
    try:
        narrated = detect_narrated_tool_call(assistant_text)
        if narrated is not None:
            narrated_name = narrated.tool_name
            event_body = narrated.raw_fence
    except Exception:
        pass
    title = (
        f"model narrated {narrated_name} call as text"
        if narrated_name
        else "model declined to call a tool when one was expected"
    )
    try:
        # Local import mirrors the pattern used by the cron
        # runner and detach worker — keeps the agent-loop module
        # event-log-agnostic so tests can supply a simple mock.
        from .events import new_entry as new_event

        events.append(
            new_event(
                kind="tool_call_narrated",
                session_key=session_key,
                title=title,
                body=event_body,
                meta={
                    "alias": alias,
                    "tool_name": narrated_name,
                    "iterations": iterations,
                },
            )
        )
    except Exception as exc:
        _log.warning("capabilities.narrated_emit_failed", extra={"error": str(exc)})


# --------------------------------------------------------------- the loop


async def run_agent_loop(
    *,
    alias: str,
    messages: list[dict[str, Any]],
    request_body_extras: dict[str, Any] | None = None,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    session_key: str,
    max_iterations: int = _MAX_TOOL_CALL_ITERATIONS,
) -> AgentLoopResult:
    """Run the tool-use loop to a natural stop.

    Parameters:

    * ``alias`` — FITT alias to dispatch against (``fitt-smart``
      etc). Resolved per iteration via ``alias_router``.
    * ``messages`` — OpenAI-shape message list. Copied once; the
      loop appends assistant + tool-result messages as it goes
      and returns the final list via ``result.messages``.
    * ``request_body_extras`` — passed through to the dispatch
      body untouched. The chat endpoint uses it to forward
      things like ``temperature``, ``tools`` (with FITT's
      appended), and ``tool_choice``.
    * ``alias_router`` / ``tool_registry`` / ``approval`` /
      ``tool_ctx`` — the loop's collaborators, all caller-owned
      so tests (and cron firings) can supply test doubles.
    * ``session_key`` — used for audit + gap entries and logged
      on the per-tool info line.

    Does NOT touch memory. Callers that want persistence (the
    chat endpoint, the cron runner) should call
    ``memory.append_turn(session_key, user_message, result.assistant_text)``
    themselves after inspecting ``result.status``. Why? The
    "what counts as the user message" is context-specific — chat
    has it from the request, cron uses the cron's stored prompt,
    a future subagent would pass its parent's instruction.

    Capability-gap logging is driven by ``tool_ctx.events``'
    sibling hook on the context: ``tool_ctx``-attached objects
    are read lazily by :func:`record_gap`, which the caller
    runs after this returns. Same separation-of-concerns reason
    as memory.
    """
    working_body = dict(request_body_extras or {})
    working_messages = list(messages)
    model_used = None
    fallback_used = False
    in_tok_total = 0
    out_tok_total = 0
    response_obj: Any = None
    # Phase 5 — accumulator for tool calls this turn ran. Passed
    # to memory.append_turn so structured call records persist
    # alongside the user + assistant message pair. One entry per
    # executed call; order matches the model's tool_calls list.
    tool_calls_for_memory: list[PersistedToolCall] = []

    iterations = 0
    for iteration in range(max_iterations):
        iterations = iteration + 1
        working_body["messages"] = working_messages
        try:
            dispatch = await alias_router.dispatch(alias, working_body)
        except (UnknownAlias, NoBackendAvailable):
            raise
        except Exception as exc:
            return AgentLoopResult(
                status="upstream_error",
                response_obj=response_obj,
                model_used=model_used,
                fallback_used=fallback_used,
                in_tokens=in_tok_total,
                out_tokens=out_tok_total,
                iterations=iterations,
                error=exc,
                messages=working_messages,
            )

        response_obj = dispatch.response
        model_used = dispatch.model_used
        fallback_used = fallback_used or dispatch.fallback_used
        in_tok, out_tok = extract_usage(response_obj)
        in_tok_total += in_tok
        out_tok_total += out_tok

        tool_calls = extract_tool_calls(response_obj)
        if not tool_calls:
            break  # natural stop — model produced a final reply

        assistant_msg = assistant_message_from_response(response_obj)
        if assistant_msg is not None:
            working_messages.append(assistant_msg)

        for call in tool_calls:
            tool_started = time.perf_counter()
            call_id, result, decision, tool = await execute_tool_call(
                call,
                registry=tool_registry,
                approval=approval,
                tool_ctx=tool_ctx,
            )
            duration_ms = int((time.perf_counter() - tool_started) * 1000)
            _log.info(
                "tool.invoked",
                extra={
                    "tool": tool.name if tool else "(unknown)",
                    "decision": decision.reason,
                    "ok": not result.is_error,
                    "session_id": session_key,
                    "iteration": iteration,
                },
            )
            audit_log = tool_ctx.audit
            if audit_log is not None:
                try:
                    audit_log.append(
                        new_audit_entry(
                            session_key=session_key,
                            client=tool_ctx.client,
                            tool=tool.name if tool else "(unknown)",
                            args=tool_call_args(call),
                            decision=decision.reason,
                            ok=not result.is_error,
                            duration_ms=duration_ms,
                            error=result.payload if result.is_error else "",
                            extra={"iteration": iteration},
                        )
                    )
                except Exception as e:
                    _log.warning("audit.append_failed", extra={"error": str(e)})
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result.payload,
                }
            )
            # Phase 5 — record the call for persistence.
            tool_calls_for_memory.append(
                _persisted_tool_call_from_result(
                    call=call,
                    tool=tool,
                    result=result,
                )
            )
    else:
        return AgentLoopResult(
            status="tool_loop_exhausted",
            response_obj=response_obj,
            model_used=model_used,
            fallback_used=fallback_used,
            in_tokens=in_tok_total,
            out_tokens=out_tok_total,
            iterations=iterations,
            messages=working_messages,
            tool_calls_for_memory=tool_calls_for_memory,
        )

    return AgentLoopResult(
        status="ok",
        assistant_text=extract_assistant_text(response_obj),
        response_obj=response_obj,
        model_used=model_used,
        fallback_used=fallback_used,
        in_tokens=in_tok_total,
        out_tokens=out_tok_total,
        iterations=iterations,
        messages=working_messages,
        tool_calls_for_memory=tool_calls_for_memory,
    )


def _persisted_tool_call_from_result(
    *,
    call: dict[str, Any],
    tool: Tool | None,
    result: ToolResult,
) -> PersistedToolCall:
    """Build a :class:`PersistedToolCall` from a live tool
    invocation's inputs + result.

    * ``tool_name``: from the tool's registry entry (or the
      raw call's name if the tool wasn't found in the registry).
    * ``args``: the structured arguments dict as the model
      emitted them. The 2026-05-11 correction replaces the
      earlier ``args_summary`` string: the summary was lossy
      and couldn't round-trip, producing the
      ``_persisted_args`` placeholder that poisoned
      subsequent turns (see docs/observed-issues.md).
    * ``result_status`` / ``result_summary``: derived from the
      tool's :class:`ToolResult`. Success = ``ok``; error =
      ``error`` plus the first ~300 chars of the payload; the
      shell tools' exit-code convention is detected and
      surfaced as ``exit=N`` (``project_shell`` writes
      ``exit=N\\n\\n...`` in its payload — parse that shape
      out so the persisted record is cleaner).
    """
    tool_name = tool.name if tool is not None else call.get("function", {}).get("name", "(unknown)")
    args = _extract_args_dict(call)
    status, summary = _status_and_summary_from_result(result)
    return PersistedToolCall(
        tool_name=tool_name,
        args=args,
        result_status=status,
        result_summary=summary,
    )


def _extract_args_dict(call: dict[str, Any]) -> dict[str, Any]:
    """Parse the model's emitted arguments from a tool-call
    dict into a structured Python dict.

    OpenAI's contract: ``function.arguments`` is a JSON
    string. We decode it so the persisted record carries the
    real structure. On malformed input (model emitted
    non-JSON, or the arguments field is the wrong type) we
    persist an empty dict and move on — the rest of the turn
    still completes, and the audit log has the raw input if
    forensics are needed."""
    raw = call.get("function", {}).get("arguments", "{}")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _status_and_summary_from_result(result: ToolResult) -> tuple[str, str]:
    """Extract a short ``(status, summary)`` from a tool
    result.

    Success → ``("ok", "")``. Error → ``(status, summary)``
    where status parses out the common shell convention
    (``exit=N\\n\\n...`` from ``project_shell``) when
    present, otherwise falls back to ``"error"``. Summary
    caps at 300 chars so tomorrow's context stays compact
    even for verbose failures."""
    if not result.is_error:
        return "ok", ""
    payload = result.payload or "error"
    # Shell-ish payloads start with "exit=N\n\n..." — lift the
    # status out so the persisted record carries both the
    # exit code and a short tail of stderr, not just "error".
    status = "error"
    summary = payload
    if payload.startswith("exit="):
        first_newline = payload.find("\n")
        if first_newline != -1:
            status = payload[:first_newline].strip()
            summary = payload[first_newline:].strip()
        else:
            status = payload.strip()
            summary = ""
    if len(summary) > 300:
        summary = summary[:297] + "..."
    return status, summary

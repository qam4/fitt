"""POST /v1/chat/completions - the main gateway endpoint.

Responsibilities:

* Validate the request shape (Pydantic).
* Reject concrete model IDs (must be aliases).
* Resolve the active session id from the X-FITT-Session header.
* Load memory (identity + today's history) and inject it into the
  request before dispatch.
* Call the AliasRouter for actual dispatch.
* Translate upstream errors per the design.md failure-handling table.
* Return either a plain JSON response or an SSE stream, preserving
  the upstream chunk order byte-for-byte modulo OpenAI envelope
  rewriting.
* Append the completed turn to memory after a successful response.
* Emit one structured log event per request.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from .agent_loop import (
    _MAX_TOOL_CALL_ITERATIONS,
    record_gap,
    response_to_dict,
    run_agent_loop,
)
from .auth import is_router_mode_client
from .capabilities import build_capability_block
from .config import Config
from .cost import estimate_cost
from .detach import (
    PLACEHOLDER_MESSAGE,
    DetachedPending,
    build_placeholder_response,
    finish_detached,
    run_with_detach,
)
from .errors import ModelIdNotAlias, NoBackendAvailable, UnknownAlias
from .logging_config import get_logger, log_request
from .memory import LoadedContext, MemoryStore
from .models import ChatCompletionRequest
from .router import AliasRouter, backend_tag
from .sessions import SessionRegistry, resolve_session_id
from .tools import ToolContext, ToolRegistry

router = APIRouter()
_log = get_logger("fitt.gateway.chat")


# -------- request parsing -----------------------------------------


async def _parse_request(request: Request, config: Config) -> ChatCompletionRequest:
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

    # Debug-only request-body logging. Off by default because
    # bodies contain user prompts. Flip ``server.log_bodies:
    # true`` in config.yaml when diagnosing
    # "why does this client's request behave differently from
    # that one" mysteries — exactly the kind of debugging that
    # caught the 2026-05-08 Continue-vs-Telegram tool-call
    # divergence where the same model behaved differently
    # depending on what Continue's agent mode injected into the
    # system prompt. Logs the client tag so you can grep for a
    # specific interface's requests.
    if getattr(config.server, "log_bodies", False):
        client_tag = getattr(request.state, "client", "unknown")
        _log.info(
            "chat.request_body",
            client=client_tag,
            body=payload,
        )

    try:
        parsed = ChatCompletionRequest.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e

    # Reject concrete model IDs - client must use aliases.
    aliases = config.alias_names()
    if parsed.model not in aliases:
        concrete_shapes = (
            "/" in parsed.model
            or ":" in parsed.model
            or parsed.model.startswith(("claude-", "gpt-", "qwen"))
        )
        if concrete_shapes:
            raise ModelIdNotAlias(parsed.model, aliases)
        raise UnknownAlias(parsed.model, aliases)

    return parsed


# -------- memory injection ----------------------------------------


def _inject_memory(
    body: dict[str, Any],
    ctx: LoadedContext,
    *,
    capability_block: str = "",
    skills_block: str = "",
) -> dict[str, Any]:
    """Return a shallow copy of ``body`` with memory (and
    optionally a capability block + skills block) prepended.

    * If ``capability_block`` is non-empty, it becomes the first
      part of the system prefix — before identity/lessons —
      because the model's *ability list* is the most urgent
      piece of context: it stops tool-name hallucination and
      drives the ``I'd need a tool to ...`` gap-reporting
      phrasing we hook on the reply side.
    * If ``skills_block`` is non-empty, it goes immediately
      after the capability block and before identity/lessons.
      Phase 4.10: the two are conceptually paired — "what you
      can do directly" then "what you can do via recipes" —
      and live next to each other in the system prompt.
    * If ``ctx.system_prefix`` is non-empty, it's merged into the
      system message. If the request already has a system message,
      memory content is appended to it with a separator; otherwise
      a new system message is inserted at position 0.
    * ``ctx.history_messages`` are inserted just before the last
      (user) message in the request. We don't assume the list is
      already in any specific shape beyond OpenAI's
      ``[{"role", "content"}, ...]``.

    The original ``body`` is not mutated.
    """
    messages: list[dict[str, Any]] = list(body.get("messages") or [])

    # Stack the prefix layers in order: capabilities, skills,
    # identity+lessons. Drop empty strings so we don't end up
    # with stray blank-line separators in the system message.
    prefix_parts = [p for p in (capability_block, skills_block, ctx.system_prefix) if p]
    system_prefix = "\n\n".join(prefix_parts)

    if not system_prefix and not ctx.history_messages:
        return body

    new_messages: list[dict[str, Any]] = []

    # Handle system message first.
    if messages and messages[0].get("role") == "system":
        merged_system = {
            "role": "system",
            "content": _merge_system(system_prefix, messages[0].get("content", "")),
        }
        new_messages.append(merged_system)
        rest = messages[1:]
    else:
        if system_prefix:
            new_messages.append({"role": "system", "content": system_prefix})
        rest = messages

    # History comes before the remaining (new) user message(s).
    new_messages.extend(ctx.history_messages)
    new_messages.extend(rest)

    out = dict(body)
    out["messages"] = new_messages
    return out


def _merge_system(prefix: str, existing: Any) -> str:
    """Concatenate the memory's system prefix with any existing
    system content on the request."""
    existing_str = existing if isinstance(existing, str) else ""
    if prefix and existing_str:
        return f"{prefix}\n\n---\n\n{existing_str}"
    return prefix or existing_str


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    """Pull the user-visible text from the last user message.

    OpenAI content can be a string or a list of parts; we accept
    both but only persist text parts in memory.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "\n".join(parts).strip()
    return ""


# -------- upstream error translation ------------------------------


def _translate_upstream_error(
    exc: Exception,
    *,
    upstream_timeout_secs: float | None = None,
    alias: str | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    """Map an exception from LiteLLM to the right HTTP response.

    * ``litellm.Timeout`` (Phase 4.9) -> 503 ``upstream_silent``
    * 429 / 529 (rate-limit / overload) -> 503 + Retry-After
    * 4xx other                         -> pass through with body
    * 5xx other                         -> 502 + upstream message

    The ``upstream_timeout_secs`` / ``alias`` / ``request_id``
    kwargs are required when the caller wants the
    ``upstream_silent`` shape — they populate the structured
    error body the bot's ``_format_error`` reads to build a
    user-facing string. For the other branches they're ignored
    (callers that don't have them set just don't pass them).
    """
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    retry_after = None

    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")

    # Phase 4.9: LiteLLM's Timeout. Same routing as
    # _classify_upstream_error so the wire shape and the log
    # shape can't drift.
    is_litellm_timeout = type(exc).__name__ == "Timeout" or (
        status == 408 and "timeout" in message.lower()
    )
    if is_litellm_timeout and upstream_timeout_secs is not None and alias is not None:
        return _upstream_silent_response(
            timeout_secs=upstream_timeout_secs,
            alias=alias,
            request_id=request_id or "",
        )

    if status in (429, 529):
        retry_after = retry_after or ("30" if status == 529 else "5")
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "upstream_rate_limited",
                    "message": message,
                    "upstream_status": status,
                }
            },
            headers={"Retry-After": str(retry_after)},
        )

    if isinstance(status, int) and 400 <= status < 500:
        return JSONResponse(
            status_code=status,
            content={"error": {"type": "upstream_client_error", "message": message}},
        )

    return JSONResponse(
        status_code=502,
        content={"error": {"type": "upstream_server_error", "message": message}},
    )


def _classify_upstream_error(exc: Exception) -> dict[str, Any]:
    """Return a structured classification of an upstream-dispatch
    exception, suitable for ``log_request``'s extra fields.

    Mirrors the routing logic of :func:`_translate_upstream_error`
    so the structured log shape and the user-facing HTTP shape
    can't drift. Four buckets:

    * ``upstream_silent`` — LiteLLM (or the underlying httpx) hit
      our configured timeout. The upstream went quiet for longer
      than we were willing to wait. Phase 4.9.
    * ``upstream_rate_limited`` — 429/529 from upstream.
      ``upstream_status`` carries the actual code; ``retry_after``
      records the parsed Retry-After header (or the synthesized
      default).
    * ``upstream_client_error`` — other 4xx (auth, bad request).
    * ``upstream_server_error`` — 5xx, transport failures
      (connection reset, read timeout), DNS failures, anything
      that doesn't expose a ``status_code`` attribute. Catch-all.

    The ``error_class`` field carries the Python exception class
    name (``RateLimitError``, ``APIConnectionError``,
    ``httpx.ReadTimeout``, etc.) so an operator grepping the log
    can tell "is this NVIDIA queue depth or my Tailscale flapping
    or both?". The ``error_detail`` field carries the
    exception's str() so the wire-side detail (NVIDIA's
    "368 in queue" body, say) is preserved without needing to
    enable response-body capture.
    """
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    resp = getattr(exc, "response", None)
    retry_after: str | None = None
    if resp is not None:
        headers = getattr(resp, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")

    fields: dict[str, Any] = {
        "error_class": type(exc).__name__,
        "error_detail": message[:500] if isinstance(message, str) else str(message)[:500],
    }
    # Phase 4.9: LiteLLM raises ``litellm.Timeout`` (with
    # ``status_code=408`` per their convention) when the
    # ``timeout=`` kwarg fires. We treat that as upstream_silent
    # rather than letting it fall through to upstream_client_error
    # — 408 from the upstream itself would be unusual and is
    # operationally the same shape (we couldn't get an answer
    # in time).
    is_litellm_timeout = type(exc).__name__ == "Timeout" or (
        status == 408 and "timeout" in message.lower()
    )
    if is_litellm_timeout:
        fields["error_type"] = "upstream_silent"
        return fields
    if status in (429, 529):
        fields["error_type"] = "upstream_rate_limited"
        fields["upstream_status"] = status
        fields["retry_after"] = retry_after or ("30" if status == 529 else "5")
    elif isinstance(status, int) and 400 <= status < 500:
        fields["error_type"] = "upstream_client_error"
        fields["upstream_status"] = status
    else:
        fields["error_type"] = "upstream_server_error"
        if isinstance(status, int):
            fields["upstream_status"] = status
    return fields


def _upstream_silent_response(
    *,
    timeout_secs: float,
    alias: str,
    request_id: str,
) -> JSONResponse:
    """Return a 503 telling the bot the upstream went silent.

    Phase 4.9: when the gateway's configured upstream timeout
    fires before the upstream responds, the bot needs an
    actionable message rather than its own ``ReadTimeout``
    trying to mean too many things at once. This is the typed
    error shape the bot's ``_format_error`` branches on
    (``error.type == "upstream_silent"``) to produce a
    user-facing string that mentions the alias, the timeout,
    and the request_id short tag.

    The 503 status code matches the existing
    ``_translate_upstream_error`` convention for
    rate-limited/overloaded — both shapes are "upstream is
    not currently giving us a response", different reasons.
    """
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "upstream_silent",
                "timeout_secs": timeout_secs,
                "alias": alias,
                "request_id": request_id,
                "message": (
                    f"Upstream {alias!r} went silent after {int(timeout_secs)}s "
                    "— likely queued or overloaded."
                ),
            }
        },
    )


# -------- streaming helpers ---------------------------------------


async def _sse_stream_with_memory(
    upstream: AsyncIterator[Any],
    *,
    memory: MemoryStore,
    session_id: str,
    user_message: str,
) -> AsyncIterator[bytes]:
    """Forward LiteLLM streaming chunks as OpenAI SSE bytes AND
    collect the assistant's content so we can append the turn after
    ``[DONE]``.

    On mid-stream error: emit ``[ERROR]`` and do NOT append the
    partial content. A partial response is not a conversation turn.
    """
    collected: list[str] = []
    succeeded = False
    try:
        async for chunk in upstream:
            if hasattr(chunk, "model_dump"):
                payload = chunk.model_dump(exclude_none=True)
            elif isinstance(chunk, dict):
                payload = chunk
            else:
                payload = {"raw": str(chunk)}
            # Collect text deltas for memory append.
            delta = _extract_stream_delta(payload)
            if delta:
                collected.append(delta)
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"
        succeeded = True
    except Exception as exc:
        err = {"error": {"type": "stream_failure", "message": str(exc)}}
        yield f"data: {json.dumps(err)}\n\n".encode()
        yield b"data: [ERROR]\n\n"
        # Mid-stream failures are easy to miss otherwise: the
        # client sees ``[ERROR]`` on the wire, but the gateway
        # would log nothing structured because we'd already
        # emitted the ``stream_started`` ``chat.completion``
        # event at dispatch time. This second event closes the
        # loop with the actual outcome.
        _log.warning(
            "chat.completion",
            session_id=session_id,
            status="stream_failure",
            error_class=type(exc).__name__,
            error_detail=str(exc)[:500],
            collected_chars=len("".join(collected)),
        )

    if succeeded and collected:
        assistant_message = "".join(collected)
        try:
            memory.append_turn(session_id, user_message, assistant_message)
        except Exception as exc:  # disk-full, permission, ...
            _log.warning(
                "memory.append_failed",
                session_id=session_id,
                error=str(exc),
            )


def _extract_stream_delta(chunk: dict[str, Any]) -> str:
    """Pull the assistant content fragment from one OpenAI-format
    streaming chunk. Returns "" if the chunk doesn't carry text."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    if isinstance(content, str):
        return content
    return ""


# -------- extracting token counts and response text ---------------


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from a LiteLLM response."""
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


def _extract_assistant_text(response: Any) -> str:
    """Pull the assistant's text content from a non-streaming
    LiteLLM response."""
    if response is None:
        return ""
    if hasattr(response, "model_dump"):
        response = response.model_dump(exclude_none=True)
    if not isinstance(response, dict):
        return ""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return ""


# -------- tool forwarding -----------------------------------------


def _inject_fitt_tools(body: dict[str, Any], registry: ToolRegistry) -> dict[str, Any]:
    """Append FITT's registered tools to the request's ``tools`` array.

    Client-supplied tools (e.g. Continue's Agent-mode toolkit) are
    preserved and come *first* so client-owned names take lookup
    precedence when the names happen to collide. We don't de-dupe
    beyond that — the chat dispatcher routes by exact name match
    against FITT's registry, so anything we don't own is handed
    back to the client verbatim.
    """
    out = dict(body)
    client_tools = list(body.get("tools") or [])
    fitt_tools = [t.to_openai_schema() for t in registry.list_all()]
    out["tools"] = client_tools + fitt_tools
    return out


def _response_to_dict(response: Any) -> dict[str, Any] | None:
    # Re-export for anything that imports from chat. New code
    # should import from gateway.agent_loop directly.
    return response_to_dict(response)


def _build_tool_context(request: Request) -> ToolContext:
    """Assemble the ToolContext from app.state + the Bearer-auth'd
    client tag the auth middleware stashed on request.state."""
    client = getattr(request.state, "client", "unknown")
    # Session id is resolved earlier in the chat handler; we pass
    # "main" as a safe default when this helper is used in test
    # contexts that haven't resolved one yet. Real call sites
    # override by overriding ToolContext directly.
    return ToolContext(
        client=client,
        session_key="main",
        projects=request.app.state.project_registry,
        backend=request.app.state.execution_backend,
        policy=request.app.state.tool_registry.policy,
        audit=getattr(request.app.state, "audit", None),
        cron=getattr(request.app.state, "cron", None),
        events=getattr(request.app.state, "events", None),
    )


def _record_gap(request: Request, assistant_text: str, session_key: str) -> None:
    """Parse the assistant reply for a capability-gap statement
    and append to the log. All errors are swallowed with a
    warning — gap logging must never break a successful chat."""
    gap_log = getattr(request.app.state, "capability_gaps", None)
    tool_registry = getattr(request.app.state, "tool_registry", None)
    record_gap(gap_log, assistant_text, session_key, tool_registry=tool_registry)


async def _peek_latest_pending_tool(approval: Any, session_id: str) -> tuple[str, str]:
    """Look up the most-recent pending approval for this session.

    Used at detach time to attach a tool name + approval id to
    the eventual ``late_tool_result`` / ``late_tool_rejected``
    event's meta. Strictly best-effort — the loop may chain more
    than one tool before detaching and the middleware doesn't
    expose per-session history, so the "latest" pending is our
    best guess. Returns ``("", "")`` when the approval has
    already resolved (race between detach and user tap).
    """
    try:
        pending = await approval.list_pending()
    except Exception:
        return "", ""
    for entry in reversed(pending):
        if getattr(entry, "session_key", "") == session_id:
            return entry.tool_name, entry.approval_id
    return "", ""


def _push_channel_available(request: Request) -> bool:
    """Is there a push subscriber the detached worker can reach?

    v0 heuristic: the gateway has a Telegram bot token
    configured. If so, the bot *might* be running; if not, the
    only consumer is ``fitt inbox``. We warn at detach time
    when there's no channel so the operator knows their late
    event is only visible via the CLI."""
    secrets = getattr(request.app.state.config, "secrets", None)
    if secrets is None:
        return False
    return getattr(secrets, "telegram", None) is not None


async def _run_tool_loop(
    *,
    parsed: ChatCompletionRequest,
    session_id: str,
    user_message_for_memory: str,
    request_body: dict[str, Any],
    ctx: LoadedContext,
    memory: MemoryStore,
    alias_router: AliasRouter,
    tool_registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
    wanted_stream: bool,
    started: float,
    upstream_timeout_secs: float,
    request_id: str,
    capability_gaps: Any = None,
    events: Any = None,
    push_channel_available: bool = True,
    artifact_store: Any = None,
    context_windows: Any = None,
    turn_capture_store: Any = None,
    traceability_default_capture: list[str] | None = None,
    traceability_enabled: bool = True,
) -> Response:
    """Dispatch, execute tool calls, re-dispatch, repeat, then return.

    Thin HTTP-specific wrapper around :func:`run_agent_loop`. The
    loop itself is stateless and reused by cron firings (Phase 4.5)
    and the spec runner (Phase 6); this function handles the
    concerns that only apply to a live HTTP request — memory
    persistence on the original turn, response envelope shaping,
    request logging, and stream wrapping.

    Detached delivery (Phase 4.5 Task 5.5): if the policy
    configures ``tools.approval_detach_threshold_secs`` and the
    loop is still running at that threshold (almost always
    because it's waiting on a human approval), the handler
    returns a placeholder response immediately and hands the
    remaining work off to a background worker that emits a
    ``late_tool_result`` / ``late_tool_rejected`` event.
    """
    # Strip the messages out of request_body so we don't pass
    # them twice to run_agent_loop. Everything else (tools,
    # tool_choice, temperature, ...) flows through unchanged.
    body_extras = {k: v for k, v in request_body.items() if k != "messages"}
    original_messages: list[dict[str, Any]] = list(request_body.get("messages") or [])

    # Phase 4.8: emit turn_started now that we've committed to
    # running the tool loop for this request. Emission is a
    # no-op when tool_ctx.turns or turn_id is None (tests, or
    # early-phase callers that haven't wired TurnLog yet).
    from .turn_events import record_turn_started

    record_turn_started(
        getattr(tool_ctx, "turns", None),
        getattr(tool_ctx, "turn_id", None),
        session_id,
        alias=parsed.model,
        client=tool_ctx.client,
        user_msg_len=len(user_message_for_memory or ""),
    )

    detach_threshold = tool_registry.policy.approval_detach_threshold_secs

    def _build_loop_coro():  # type: ignore[no-untyped-def]
        return run_agent_loop(
            alias=parsed.model,
            messages=original_messages,
            request_body_extras=body_extras,
            alias_router=alias_router,
            tool_registry=tool_registry,
            approval=approval,
            tool_ctx=tool_ctx,
            session_key=session_id,
            artifact_store=artifact_store,
        )

    async def _on_detach(task: Any) -> None:
        """Scheduled by run_with_detach when the threshold fires.

        The tool loop is still running (``asyncio.shield`` keeps
        it alive); we hand the same task reference to
        :func:`finish_detached`, which awaits it and emits the
        right event when the loop eventually completes."""
        if not push_channel_available:
            _log.warning(
                "chat.detach.no_push_channel",
                extra={
                    "session_id": session_id,
                    "note": (
                        "no Telegram / push subscriber configured; "
                        "late_tool_result will only be visible via "
                        "fitt inbox"
                    ),
                },
            )
        # Try to pull a tool name from the most recent pending
        # approval for this session. Best-effort: the exact tool
        # is unknowable across detach (the loop might chain
        # several) but surfacing *a* tool name beats "(unknown)"
        # on the Telegram preview.
        tool_name, approval_id = await _peek_latest_pending_tool(approval, session_id)
        await finish_detached(
            task,
            session_key=session_id,
            user_message=user_message_for_memory,
            events=events,
            memory=memory,
            tool_name=tool_name,
            approval_id=approval_id,
            original_client=tool_ctx.client,
            push_channel_available=push_channel_available,
        )

    try:
        outcome = await run_with_detach(
            _build_loop_coro,
            detach_threshold_s=detach_threshold,
            on_detach=_on_detach,
        )
    except UnknownAlias:
        raise
    except NoBackendAvailable as exc:
        # Tool-loop counterpart to the same observability fix
        # in the plain-chat dispatch path: log every transport
        # failure that emptied the candidate chain so an
        # operator can correlate "telegram says gateway
        # unreachable" with the actual ``ConnectError`` /
        # ``ReadTimeout`` underneath.
        #
        # Phase 4.9: same upstream_silent vs no_backend split as
        # the plain-chat path — when the cause is a LiteLLM
        # ``Timeout``, surface the typed ``upstream_silent``
        # shape to the bot so the user sees an actionable
        # message.
        latency_ms = int((time.perf_counter() - started) * 1000)
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        classification = _classify_upstream_error(cause)
        is_silent = classification.get("error_type") == "upstream_silent"
        status_for_log = "upstream_silent" if is_silent else "no_backend_available"
        extras = {k: v for k, v in classification.items() if k != "error_type"}
        if is_silent:
            extras["timeout_secs"] = upstream_timeout_secs
        log_request(
            _log,
            alias=parsed.model,
            model="(unknown)",
            backend="(unknown)",
            backend_actual="(unknown)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status=status_for_log,
            fallback=False,
            attempted=list(exc.attempted),
            **extras,
        )
        if is_silent:
            return _upstream_silent_response(
                timeout_secs=upstream_timeout_secs,
                alias=parsed.model,
                request_id=request_id,
            )
        raise

    # ---- detached: return a placeholder and let the worker finish
    if isinstance(outcome, DetachedPending):
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_request(
            _log,
            alias=parsed.model,
            model="(detached)",
            backend="(detached)",
            backend_actual="(detached)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status="detached",
            fallback=False,
        )
        placeholder_body = build_placeholder_response(model=parsed.model)
        headers = {
            "X-FITT-Backend": "(detached)",
            "X-FITT-Alias": parsed.model,
            "X-FITT-Session": session_id,
            "X-FITT-Detached": "1",
        }
        if wanted_stream:
            # Streaming clients get the placeholder as a single
            # content delta frame + stop terminator, matching the
            # shape the tool loop uses for natural-stop
            # responses.
            def _chunk(delta: dict[str, Any] | None, finish_reason: str | None) -> dict[str, Any]:
                return {
                    "id": placeholder_body["id"],
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta or {},
                            "finish_reason": finish_reason,
                        }
                    ],
                }

            first = _chunk({"role": "assistant", "content": PLACEHOLDER_MESSAGE}, None)
            final = _chunk({}, "stop")

            async def _single_chunk() -> AsyncIterator[bytes]:
                yield f"data: {json.dumps(first)}\n\n".encode()
                yield f"data: {json.dumps(final)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                _single_chunk(),
                media_type="text/event-stream",
                headers=headers,
            )
        return JSONResponse(content=placeholder_body, headers=headers)

    # ---- synchronous happy path (also covers tool_loop_exhausted
    # and upstream_error below) ------------------------------------
    result = outcome

    # Phase 4.8: emit turn_finished before every return in this
    # block so the renderer's finish footer always lands.
    from .turn_events import record_turn_finished

    _turns = getattr(tool_ctx, "turns", None)
    _turn_id = getattr(tool_ctx, "turn_id", None)

    if result.status == "upstream_error" and result.error is not None:
        record_turn_finished(
            _turns,
            _turn_id,
            session_id,
            status="upstream_error",
            iterations=result.iterations,
            final_reply_len=0,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        model_used = result.model_used
        # Without this, an upstream rate-limit / queue / server
        # error during a tool-loop turn vanished from
        # ``gateway.log`` — the chat handler returned the
        # translated 503/502 to the client without recording
        # what happened. After 2026-05-13 we always emit one
        # ``chat.completion`` event per chat request, regardless
        # of outcome, so operators can grep the log for failed
        # turns and tell e.g. NVIDIA queue depth from a
        # transient connection drop.
        classification = _classify_upstream_error(result.error)
        log_request(
            _log,
            alias=parsed.model,
            model=model_used.model if model_used else "(unknown)",
            backend=model_used.backend if model_used else "(unknown)",
            backend_actual=backend_tag(model_used) if model_used else "(unknown)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=result.in_tokens,
            output_tokens=result.out_tokens,
            cost_usd=Decimal("0"),
            status=classification["error_type"],
            fallback=result.fallback_used,
            **{k: v for k, v in classification.items() if k != "error_type"},
        )
        return _translate_upstream_error(
            result.error,
            upstream_timeout_secs=upstream_timeout_secs,
            alias=parsed.model,
            request_id=request_id,
        )

    if result.status == "tool_loop_exhausted":
        latency_ms = int((time.perf_counter() - started) * 1000)
        model_used = result.model_used
        log_request(
            _log,
            alias=parsed.model,
            model=model_used.model if model_used else "(unknown)",
            backend=model_used.backend if model_used else "(unknown)",
            backend_actual=backend_tag(model_used) if model_used else "(unknown)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=result.in_tokens,
            output_tokens=result.out_tokens,
            cost_usd=Decimal("0"),
            status="tool_loop_exhausted",
            fallback=result.fallback_used,
        )
        record_turn_finished(
            _turns,
            _turn_id,
            session_id,
            status="tool_loop_exhausted",
            iterations=result.iterations,
            final_reply_len=0,
        )
        return JSONResponse(
            status_code=504,
            content={
                "error": {
                    "type": "tool_loop_exhausted",
                    "message": (
                        f"tool-call loop did not terminate within "
                        f"{_MAX_TOOL_CALL_ITERATIONS} iterations"
                    ),
                }
            },
        )

    # ---- build the final response shape ---------------------------
    model_used = result.model_used
    assistant_text = result.assistant_text
    response_obj = result.response_obj
    record_turn_finished(
        _turns,
        _turn_id,
        session_id,
        status="ok",
        iterations=result.iterations,
        final_reply_len=len(assistant_text or ""),
    )

    # Phase 7 Slice 7.2: per-turn traceability capture.
    # Fire-and-forget on the asyncio loop so the chat handler
    # returns its response without waiting on disk IO. Privacy
    # gate via traceability config — coding-agent skips by
    # default. ``tool_calls_for_memory`` is the structured tool
    # chain Phase 5 already accumulates; we wrap each entry as a
    # CapturedToolCall (the additional decision/duration data
    # isn't currently threaded through the agent loop, so v0
    # captures the structural detail and leaves
    # decision/duration as best-effort defaults; future commit
    # can extend AgentLoopResult to carry them).
    if turn_capture_store is not None and _turn_id is not None:
        from .turn_capture import (
            CapturedToolCall,
            TurnCaptureBuilder,
            should_capture,
        )

        if should_capture(
            client=tool_ctx.client,
            config_default=traceability_default_capture or [],
            enabled=traceability_enabled,
        ):
            cw_tokens: int | None = None
            if context_windows is not None and model_used is not None:
                cw = context_windows.get(model_used.backend, model_used.id)
                if cw is not None:
                    cw_tokens = cw.tokens
            finish_reason = None
            response_dict = response_to_dict(response_obj) or {}
            choices = response_dict.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    fr = first.get("finish_reason")
                    if isinstance(fr, str):
                        finish_reason = fr
            # Narration warning (D4 in design.md): post-hoc
            # classifier flag, never gates anything.
            from .capabilities import is_tool_use_expected_but_none

            tools_were_offered = bool(request_body.get("tools")) or "tool_choice" in request_body
            narration_warning = is_tool_use_expected_but_none(
                assistant_text or "",
                tools_were_offered=tools_were_offered,
                finish_reason=finish_reason,
                had_real_tool_calls=bool(result.tool_calls_for_memory),
            )

            captured_tool_calls: list[CapturedToolCall] = []
            for idx, tc in enumerate(result.tool_calls_for_memory or []):
                captured_tool_calls.append(
                    CapturedToolCall(
                        call_id=f"call-{idx}",
                        tool_name=tc.tool_name,
                        args=tc.args,
                        decision=tc.result_status if tc.result_status != "ok" else "auto",
                        decision_detail="",
                        duration_ms=0,
                        ok=tc.result_status == "ok",
                        result_summary=(tc.result_summary or "")[:300],
                        artifact_path=None,
                        iteration=idx,
                    )
                )

            builder = TurnCaptureBuilder(
                turn_id=_turn_id,
                session_key=session_id,
                alias=parsed.model,
                client=tool_ctx.client,
                started_at=started,
            )
            builder.dispatched_messages = list(result.messages or [])
            builder.response = response_dict
            builder.tool_calls = captured_tool_calls
            builder.model_used = model_used.id if model_used is not None else "(unknown)"
            builder.backend = model_used.backend if model_used is not None else "(unknown)"
            builder.fallback_used = result.fallback_used
            builder.prompt_tokens = result.in_tokens
            builder.completion_tokens = result.out_tokens
            builder.context_window = cw_tokens
            builder.finish_reason = finish_reason
            builder.narration_warning = narration_warning
            builder.iterations = result.iterations
            builder.status = result.status
            try:
                turn_capture_store.write_async(builder.build())
            except RuntimeError:
                # Not on a running loop — shouldn't happen in
                # the chat handler path, but defensive. Skip
                # capture rather than crashing the turn.
                _log.debug("turn_capture.no_running_loop")

    cost = (
        estimate_cost(model_used, result.in_tokens, result.out_tokens)
        if model_used
        else Decimal("0")
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    if user_message_for_memory and assistant_text:
        try:
            memory.append_turn(
                session_id,
                user_message_for_memory,
                assistant_text,
                tool_calls=(result.tool_calls_for_memory or None),
            )
        except Exception as exc:
            _log.warning(
                "memory.append_failed",
                session_id=session_id,
                error=str(exc),
            )
    record_gap(
        capability_gaps,
        assistant_text,
        session_id,
        tool_registry=tool_registry,
    )

    backend_header = backend_tag(model_used) if model_used else "(unknown)"
    log_request(
        _log,
        alias=parsed.model,
        model=model_used.model if model_used else "(unknown)",
        backend=model_used.backend if model_used else "(unknown)",
        backend_actual=backend_header,
        session_id=session_id,
        history_messages=len(ctx.history_messages),
        history_truncated_bytes=ctx.truncated_bytes,
        latency_ms=latency_ms,
        input_tokens=result.in_tokens,
        output_tokens=result.out_tokens,
        cost_usd=cost,
        status="ok",
        fallback=result.fallback_used,
    )

    body_dict = response_to_dict(response_obj) or {}
    headers = {
        "X-FITT-Backend": backend_header,
        "X-FITT-Alias": parsed.model,
        "X-FITT-Session": session_id,
    }
    # Phase 4.8b: surface the turn_id so the Telegram bot can
    # route chat reply-token deltas into the matching
    # TurnRenderer when this turn has a growing bubble. Only
    # set for tool-loop turns (``_run_tool_loop`` is where
    # ``tool_ctx.turn_id`` exists); plain-chat turns don't
    # carry one.
    if getattr(tool_ctx, "turn_id", None):
        headers["X-FITT-Turn-Id"] = str(tool_ctx.turn_id)
    if result.fallback_used:
        headers["X-FITT-Fallback"] = "1"

    if wanted_stream:
        # The tool loop produced a non-streaming response
        # (`choices[0].message.content`). Streaming clients (bot +
        # open-webui) parse `choices[0].delta.content` instead, so
        # we rewrite the envelope to a single streaming chunk. Two
        # frames: one for the content delta, one to terminate.

        def _chunk(delta: dict[str, Any] | None, finish_reason: str | None) -> dict[str, Any]:
            return {
                "id": body_dict.get("id", "chatcmpl-fitt"),
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": delta or {},
                        "finish_reason": finish_reason,
                    }
                ],
            }

        first = _chunk(
            {"role": "assistant", "content": assistant_text},
            None,
        )
        final = _chunk({}, "stop")

        async def _single_chunk() -> AsyncIterator[bytes]:
            yield f"data: {json.dumps(first)}\n\n".encode()
            yield f"data: {json.dumps(final)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            _single_chunk(),
            media_type="text/event-stream",
            headers=headers,
        )
    return JSONResponse(content=body_dict, headers=headers)


# -------- the endpoint --------------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    config: Config = request.app.state.config
    memory: MemoryStore = request.app.state.memory
    session_registry: SessionRegistry = request.app.state.session_registry
    tool_registry: ToolRegistry = request.app.state.tool_registry
    approval = request.app.state.approval
    alias_router = AliasRouter(config)

    parsed = await _parse_request(request, config)
    session_id = resolve_session_id(request, session_registry)
    started = time.perf_counter()

    client = getattr(request.state, "client", "unknown")
    router_mode = is_router_mode_client(client)

    # ---- memory load + injection ---------------------------------
    # Router-mode clients (Aider, Claude Code, Cursor, ...) own
    # their own agent loop and their own system prompt. Injecting
    # FITT's capability block, identity, or lessons into their
    # request actively confuses the client's agent (see the Aider
    # collision in docs/observed-issues.md). Skip the injection
    # entirely; the client's system prompt reaches the model
    # verbatim.
    if router_mode:
        ctx = LoadedContext(system_prefix="", history_messages=[])
        capability_block = ""
        skills_block = ""
        request_body = parsed.to_litellm_body()
    else:
        ctx = memory.load_context(session_id)
        # Capability block goes first in the system prefix so the
        # model always knows what tools are live. Falls back to an
        # empty string (no block) when no tools are registered yet.
        capability_block = (
            build_capability_block(tool_registry) if tool_registry.list_names() else ""
        )
        # Phase 4.10: skills block sits between capabilities and
        # identity. Empty string when there are no skills loaded
        # so the caller drops the block entirely (no header, no
        # placeholder line) — see Requirement 3.4.
        from .skills import render_skills_block

        loaded_skills = getattr(request.app.state, "skills", [])
        skills_block = render_skills_block(loaded_skills, tool_registry)

        request_body = _inject_memory(
            parsed.to_litellm_body(),
            ctx,
            capability_block=capability_block,
            skills_block=skills_block,
        )

    # The memory'd body will be dispatched; we remember the original
    # user message to persist later (not the history copy).
    user_message_for_memory = _last_user_message(parsed.to_litellm_body().get("messages") or [])

    # ---- tool forwarding: decide whether to run the tool loop ----
    # Opt-in semantics: we run the tool loop only when the client
    # signals it wants tool calling, by sending either a `tools`
    # field (even empty) or a `tool_choice`. Plain chat requests
    # (Telegram, Open WebUI, curl) don't touch either field and
    # get the original streaming path unchanged. This keeps
    # backward compatibility; Phase 3.5 callers see no difference.
    #
    # Router-mode clients are never routed through the FITT tool
    # loop regardless of their ``tools`` field — the client's
    # agent is driving, FITT is a thin pass-through.
    wants_tools = ("tools" in request_body) or ("tool_choice" in request_body)
    client_disabled = (
        wants_tools and not request_body.get("tools") and not request_body.get("tool_choice")
    )
    tools_available = bool(tool_registry.list_names())
    use_tools = wants_tools and tools_available and not client_disabled and not router_mode

    if use_tools:
        # Force non-streaming for tool-using turns so we can inspect
        # tool_calls and loop. If the client asked for streaming,
        # we'll wrap the final assembled response in a single
        # SSE frame before returning.
        wanted_stream = bool(request_body.get("stream"))
        request_body = _inject_fitt_tools(request_body, tool_registry)
        request_body = dict(request_body)
        request_body["stream"] = False
        # Phase 4.8: generate a stable turn_id for the whole
        # request so turn events carry a matching handle.
        import uuid as _uuid

        turn_id = str(_uuid.uuid4())
        tool_ctx_base = ToolContext(
            client=getattr(request.state, "client", "unknown"),
            session_key=session_id,
            projects=request.app.state.project_registry,
            backend=request.app.state.execution_backend,
            policy=tool_registry.policy,
            audit=getattr(request.app.state, "audit", None),
            cron=getattr(request.app.state, "cron", None),
            events=getattr(request.app.state, "events", None),
            local_shell=getattr(request.app.state, "local_shell", None),
            lessons=getattr(request.app.state, "lessons", None),
            turns=getattr(request.app.state, "turns", None),
            turn_id=turn_id,
            web_search_backend=config.web.search_backend,
        )
        # Phase 4.9: pass cfg-derived upstream timeout +
        # request_id into the tool loop so its dispatch can
        # be wrapped in the same shielded wait_for as the
        # plain-chat path below.
        cfg: Config = request.app.state.config
        return await _run_tool_loop(
            parsed=parsed,
            session_id=session_id,
            user_message_for_memory=user_message_for_memory,
            request_body=request_body,
            ctx=ctx,
            memory=memory,
            alias_router=alias_router,
            tool_registry=tool_registry,
            approval=approval,
            tool_ctx=tool_ctx_base,
            wanted_stream=wanted_stream,
            started=started,
            upstream_timeout_secs=cfg.upstream_timeout_secs,
            request_id=getattr(request.state, "request_id", ""),
            capability_gaps=getattr(request.app.state, "capability_gaps", None),
            events=getattr(request.app.state, "events", None),
            push_channel_available=_push_channel_available(request),
            artifact_store=getattr(request.app.state, "artifact_store", None),
            context_windows=getattr(request.app.state, "context_windows", None),
            turn_capture_store=getattr(request.app.state, "turn_capture", None),
            traceability_default_capture=list(cfg.traceability.default_capture),
            traceability_enabled=cfg.traceability.enabled,
        )

    # Phase 4.9: wrap the dispatch in a shielded ``wait_for``
    # so we time out at the configured threshold and return a
    # typed ``upstream_silent`` error to the bot, while the
    # in-flight LiteLLM call keeps running under the shield.
    # v1 lets the orphan task be silently GC'd; a future
    # commit can attach a reaper to ``dispatch_task`` at the
    # ``TimeoutError`` site below with a single line of code.
    cfg = request.app.state.config
    upstream_timeout_secs = cfg.upstream_timeout_secs
    request_id = getattr(request.state, "request_id", "")
    dispatch_task: asyncio.Task[Any] = asyncio.create_task(
        alias_router.dispatch(parsed.model, request_body)
    )
    try:
        dispatch = await asyncio.wait_for(
            asyncio.shield(dispatch_task),
            timeout=upstream_timeout_secs,
        )
    except TimeoutError:
        # The upstream went silent past our threshold. Don't
        # cancel ``dispatch_task`` — let it complete naturally
        # under the shield (asyncio cancellation propagation
        # to LiteLLM/httpx is fragile to verify; v1 chose not
        # to depend on it). The orphan is bounded by LiteLLM's
        # own ``timeout=`` kwarg passed to ``acompletion`` in
        # ``router.dispatch``, so it can't outlive the
        # configured threshold by more than a small margin.
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_request(
            _log,
            alias=parsed.model,
            model="(unknown)",
            backend="(unknown)",
            backend_actual="(unknown)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status="upstream_silent",
            fallback=False,
            timeout_secs=upstream_timeout_secs,
        )
        return _upstream_silent_response(
            timeout_secs=upstream_timeout_secs,
            alias=parsed.model,
            request_id=request_id,
        )
    except UnknownAlias:
        raise
    except NoBackendAvailable as exc:
        # All candidates had transport failures. Two sub-cases
        # for the user-facing response:
        #
        # * If the underlying cause is a LiteLLM ``Timeout`` —
        #   i.e. the upstream went silent past
        #   ``upstream_timeout_secs`` and we exhausted the
        #   fallback chain on timeouts — surface the typed
        #   ``upstream_silent`` shape so the bot can show the
        #   actionable message ("upstream queued") rather than
        #   the generic ``no_backend_available``. (Phase 4.9.)
        # * Any other transport failure (ConnectError,
        #   ReadTimeout, DNS, ...) keeps the existing
        #   ``no_backend_available`` 503; that's the honest
        #   shape for "we tried every candidate and none of
        #   them are reachable".
        latency_ms = int((time.perf_counter() - started) * 1000)
        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        classification = _classify_upstream_error(cause)
        is_silent = classification.get("error_type") == "upstream_silent"
        status_for_log = "upstream_silent" if is_silent else "no_backend_available"
        extras = {k: v for k, v in classification.items() if k != "error_type"}
        if is_silent:
            extras["timeout_secs"] = upstream_timeout_secs
        log_request(
            _log,
            alias=parsed.model,
            model="(unknown)",
            backend="(unknown)",
            backend_actual="(unknown)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status=status_for_log,
            fallback=False,
            attempted=list(exc.attempted),
            **extras,
        )
        if is_silent:
            return _upstream_silent_response(
                timeout_secs=upstream_timeout_secs,
                alias=parsed.model,
                request_id=request_id,
            )
        raise
    except Exception as exc:
        # Same observability gap as the tool-loop path: an
        # upstream rate-limit / queue / connection-reset during a
        # plain-chat dispatch returned a translated 503/502 to
        # the client without any structured log entry. Now we
        # always emit one ``chat.completion`` event regardless
        # of outcome so operators can grep for "did this turn
        # actually fail upstream?" and see the answer.
        latency_ms = int((time.perf_counter() - started) * 1000)
        classification = _classify_upstream_error(exc)
        log_request(
            _log,
            alias=parsed.model,
            model="(unknown)",
            backend="(unknown)",
            backend_actual="(unknown)",
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=latency_ms,
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status=classification["error_type"],
            fallback=False,
            **{k: v for k, v in classification.items() if k != "error_type"},
        )
        return _translate_upstream_error(
            exc,
            upstream_timeout_secs=upstream_timeout_secs,
            alias=parsed.model,
            request_id=request_id,
        )

    backend_header = backend_tag(dispatch.model_used)

    # ---- streaming path ------------------------------------------
    if dispatch.stream is not None:
        headers = {
            "X-FITT-Backend": backend_header,
            "X-FITT-Alias": parsed.model,
            "X-FITT-Session": session_id,
        }
        if dispatch.fallback_used:
            headers["X-FITT-Fallback"] = "1"

        log_request(
            _log,
            alias=parsed.model,
            model=dispatch.model_used.model,
            backend=dispatch.model_used.backend,
            backend_actual=backend_header,
            session_id=session_id,
            history_messages=len(ctx.history_messages),
            history_truncated_bytes=ctx.truncated_bytes,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status="stream_started",
            fallback=dispatch.fallback_used,
        )
        return StreamingResponse(
            _sse_stream_with_memory(
                dispatch.stream,
                memory=memory,
                session_id=session_id,
                user_message=user_message_for_memory,
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    # ---- non-streaming path --------------------------------------
    response_obj = dispatch.response
    in_tok, out_tok = _extract_usage(response_obj)
    cost = estimate_cost(dispatch.model_used, in_tok, out_tok)
    latency_ms = int((time.perf_counter() - started) * 1000)

    assistant_text = _extract_assistant_text(response_obj)
    if user_message_for_memory and assistant_text:
        try:
            memory.append_turn(session_id, user_message_for_memory, assistant_text)
        except Exception as exc:
            _log.warning(
                "memory.append_failed",
                session_id=session_id,
                error=str(exc),
            )
    _record_gap(request, assistant_text, session_id)

    log_request(
        _log,
        alias=parsed.model,
        model=dispatch.model_used.model,
        backend=dispatch.model_used.backend,
        backend_actual=backend_header,
        session_id=session_id,
        history_messages=len(ctx.history_messages),
        history_truncated_bytes=ctx.truncated_bytes,
        latency_ms=latency_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        status="ok",
        fallback=dispatch.fallback_used,
    )

    if response_obj is not None and hasattr(response_obj, "model_dump"):
        body = response_obj.model_dump(exclude_none=True)
    elif isinstance(response_obj, dict):
        body = response_obj
    else:
        body = {"raw": str(response_obj)}

    headers = {
        "X-FITT-Backend": backend_header,
        "X-FITT-Alias": parsed.model,
        "X-FITT-Session": session_id,
    }
    if dispatch.fallback_used:
        headers["X-FITT-Fallback"] = "1"
    return JSONResponse(content=body, headers=headers)

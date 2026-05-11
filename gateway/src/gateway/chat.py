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
    record_claim_mismatch,
    record_gap,
    record_narrated_tool_call,
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
) -> dict[str, Any]:
    """Return a shallow copy of ``body`` with memory (and
    optionally a capability block) prepended.

    * If ``capability_block`` is non-empty, it becomes the first
      part of the system prefix — before identity/lessons —
      because the model's *ability list* is the most urgent
      piece of context: it stops tool-name hallucination and
      drives the ``I'd need a tool to ...`` gap-reporting
      phrasing we hook on the reply side.
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
    system_prefix = ctx.system_prefix
    if capability_block:
        system_prefix = (
            capability_block if not system_prefix else f"{capability_block}\n\n{system_prefix}"
        )
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


def _translate_upstream_error(exc: Exception) -> JSONResponse:
    """Map an exception from LiteLLM to the right HTTP response.

    * 429 / 529 (rate-limit / overload) -> 503 + Retry-After
    * 4xx other                         -> pass through with body
    * 5xx other                         -> 502 + upstream message
    """
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    retry_after = None

    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")

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
    capability_gaps: Any = None,
    events: Any = None,
    push_channel_available: bool = True,
    artifact_store: Any = None,
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
    except (UnknownAlias, NoBackendAvailable):
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

    if result.status == "upstream_error" and result.error is not None:
        return _translate_upstream_error(result.error)

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
    # Narrated-tool-call detector: emits an event when the turn
    # looked like it should have produced a tool call but didn't.
    # Visible in fitt inbox so operators notice when a local
    # model is silently failing the tool-use channel.
    # Shape-based: model-independent, catches JSON-fence
    # narration, TOOL_NAME: sentinels, capability false-negatives,
    # and anything else next month's model invents. See
    # capabilities.is_tool_use_expected_but_none for the rationale.
    record_narrated_tool_call(
        events,
        assistant_text,
        session_key=session_id,
        alias=parsed.model,
        iterations=result.iterations,
        tools_were_offered=True,
        had_real_tool_calls=bool(result.tool_calls_for_memory),
    )
    # Receipt cross-check: emit a ``tool_claim_mismatch`` event
    # when the assistant reply claims it ran a tool that
    # doesn't appear in this turn's tool_calls_for_memory. The
    # 2026-05-10 22:48 failure mode: "Yes, I executed the
    # edit_file tool" with zero matching runs. See
    # gateway/claim_check.py for the parsing rules.
    record_claim_mismatch(
        events,
        assistant_text,
        session_key=session_id,
        alias=parsed.model,
        tool_calls_for_memory=result.tool_calls_for_memory,
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
        request_body = parsed.to_litellm_body()
    else:
        ctx = memory.load_context(session_id)
        # Capability block goes first in the system prefix so the
        # model always knows what tools are live. Falls back to an
        # empty string (no block) when no tools are registered yet.
        capability_block = (
            build_capability_block(tool_registry) if tool_registry.list_names() else ""
        )
        request_body = _inject_memory(
            parsed.to_litellm_body(), ctx, capability_block=capability_block
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
        )
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
            capability_gaps=getattr(request.app.state, "capability_gaps", None),
            events=getattr(request.app.state, "events", None),
            push_channel_available=_push_channel_available(request),
            artifact_store=getattr(request.app.state, "artifact_store", None),
        )

    try:
        dispatch = await alias_router.dispatch(parsed.model, request_body)
    except (UnknownAlias, NoBackendAvailable):
        raise
    except Exception as exc:
        return _translate_upstream_error(exc)

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

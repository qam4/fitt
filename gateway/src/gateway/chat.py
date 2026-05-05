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

from .config import Config
from .cost import estimate_cost
from .errors import ModelIdNotAlias, NoBackendAvailable, UnknownAlias, UnknownTool
from .logging_config import get_logger, log_request
from .memory import LoadedContext, MemoryStore
from .models import ChatCompletionRequest
from .router import AliasRouter, backend_tag
from .sessions import SessionRegistry, resolve_session_id
from .tools import ApprovalDecision, Tool, ToolContext, ToolRegistry, ToolResult

router = APIRouter()
_log = get_logger("fitt.gateway.chat")


# Safety rail on tool-call loops. The model could in principle call
# tools forever; we bound it. Picked by feel - enough for a
# multi-step "read a few files, grep, summarize" turn without giving
# a runaway loop room to fester.
_MAX_TOOL_CALL_ITERATIONS = 10


# -------- request parsing -----------------------------------------


async def _parse_request(request: Request, config: Config) -> ChatCompletionRequest:
    try:
        payload = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}") from e

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
) -> dict[str, Any]:
    """Return a shallow copy of ``body`` with memory prepended.

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
    if not ctx.system_prefix and not ctx.history_messages:
        return body

    new_messages: list[dict[str, Any]] = []

    # Handle system message first.
    system_prefix = ctx.system_prefix
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
    if response is None:
        return None
    if hasattr(response, "model_dump"):
        dumped = response.model_dump(exclude_none=True)
        return dumped if isinstance(dumped, dict) else None
    if isinstance(response, dict):
        return response
    return None


def _extract_tool_calls(response: Any) -> list[dict[str, Any]]:
    """Return the model's ``tool_calls`` array from a non-streaming
    response, or [] if the response didn't request any tool."""
    dumped = _response_to_dict(response)
    if dumped is None:
        return []
    choices = dumped.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    choice0 = choices[0]
    if not isinstance(choice0, dict):
        return []
    # Finish reason tells us unambiguously whether the model wants
    # to call tools. Older libraries emit tool_calls on the message
    # without flipping finish_reason, so we also check directly.
    finish_reason = choice0.get("finish_reason")
    msg = choice0.get("message")
    if not isinstance(msg, dict):
        return []
    tool_calls = msg.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return []
    if finish_reason not in (None, "tool_calls"):
        # The model already stopped. Don't try to run tools listed
        # on a message whose finish_reason says "stop"; that's
        # ambiguous and not worth the complexity.
        return []
    return [tc for tc in tool_calls if isinstance(tc, dict)]


def _assistant_message_from_response(response: Any) -> dict[str, Any] | None:
    dumped = _response_to_dict(response)
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


async def _execute_tool_call(
    call: dict[str, Any],
    *,
    registry: ToolRegistry,
    approval: Any,
    tool_ctx: ToolContext,
) -> tuple[str, ToolResult, ApprovalDecision, Tool | None]:
    """Resolve one tool call and return (call_id, result, decision, tool).

    Any failure surface — unknown tool, bad args, approval reject —
    is expressed as a ``ToolResult`` with ``is_error=True``. The
    model reads the payload and can decide how to recover.
    """
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

    # Parse the JSON-string arguments OpenAI sends us. The model
    # occasionally emits invalid JSON; surface that as a tool error
    # rather than a 500.
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
            tool=name,
            error=str(exc),
        )
        result = ToolResult.error(f"tool {name!r} raised {type(exc).__name__}: {exc}")
    return call_id, result, decision, tool


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
    )


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
) -> Response:
    """Dispatch, execute tool calls, re-dispatch, repeat, then return.

    Non-streaming end-to-end; if the client asked for streaming
    we wrap the final response in a one-shot SSE frame + [DONE] so
    streaming readers still see what they expect. The tool-execution
    loop is bounded at _MAX_TOOL_CALL_ITERATIONS to avoid
    runaway loops.
    """
    messages: list[dict[str, Any]] = list(request_body.get("messages") or [])
    working_body = dict(request_body)
    model_used = None
    fallback_used = False
    in_tok_total = 0
    out_tok_total = 0
    response_obj: Any = None

    for iteration in range(_MAX_TOOL_CALL_ITERATIONS):
        working_body["messages"] = messages
        try:
            dispatch = await alias_router.dispatch(parsed.model, working_body)
        except (UnknownAlias, NoBackendAvailable):
            raise
        except Exception as exc:
            return _translate_upstream_error(exc)

        # We forced stream=False upstream; dispatch.response is the
        # single-shot response object.
        response_obj = dispatch.response
        model_used = dispatch.model_used
        fallback_used = fallback_used or dispatch.fallback_used
        in_tok, out_tok = _extract_usage(response_obj)
        in_tok_total += in_tok
        out_tok_total += out_tok

        tool_calls = _extract_tool_calls(response_obj)
        if not tool_calls:
            break  # model produced a final answer

        assistant_msg = _assistant_message_from_response(response_obj)
        if assistant_msg is not None:
            messages.append(assistant_msg)

        # Execute every tool call requested in this round, in order.
        for call in tool_calls:
            call_id, result, decision, tool = await _execute_tool_call(
                call,
                registry=tool_registry,
                approval=approval,
                tool_ctx=tool_ctx,
            )
            _log.info(
                "tool.invoked",
                tool=tool.name if tool else "(unknown)",
                decision=decision.reason,
                ok=not result.is_error,
                session_id=session_id,
                iteration=iteration,
            )
            # Standard OpenAI tool-result message shape.
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result.payload,
                }
            )
    else:
        # Hit the iteration cap without a natural finish. Return
        # what we have as an error; the model has been bouncing.
        latency_ms = int((time.perf_counter() - started) * 1000)
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
            input_tokens=in_tok_total,
            output_tokens=out_tok_total,
            cost_usd=Decimal("0"),
            status="tool_loop_exhausted",
            fallback=fallback_used,
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
    cost = estimate_cost(model_used, in_tok_total, out_tok_total) if model_used else Decimal("0")
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
        input_tokens=in_tok_total,
        output_tokens=out_tok_total,
        cost_usd=cost,
        status="ok",
        fallback=fallback_used,
    )

    body_dict = _response_to_dict(response_obj) or {}
    headers = {
        "X-FITT-Backend": backend_header,
        "X-FITT-Alias": parsed.model,
        "X-FITT-Session": session_id,
    }
    if fallback_used:
        headers["X-FITT-Fallback"] = "1"

    if wanted_stream:
        # The tool loop produced a non-streaming response
        # (`choices[0].message.content`). Streaming clients (bot +
        # open-webui) parse `choices[0].delta.content` instead, so
        # we rewrite the envelope to a single streaming chunk. Two
        # frames: one for the content delta, one to terminate.
        assistant_text = _extract_assistant_text(response_obj)

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

    # ---- memory load + injection ---------------------------------
    ctx = memory.load_context(session_id)
    request_body = _inject_memory(parsed.to_litellm_body(), ctx)

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
    wants_tools = ("tools" in request_body) or ("tool_choice" in request_body)
    client_disabled = (
        wants_tools and not request_body.get("tools") and not request_body.get("tool_choice")
    )
    tools_available = bool(tool_registry.list_names())
    use_tools = wants_tools and tools_available and not client_disabled

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

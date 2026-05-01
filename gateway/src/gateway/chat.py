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
from .errors import ModelIdNotAlias, NoBackendAvailable, UnknownAlias
from .logging_config import get_logger, log_request
from .memory import LoadedContext, MemoryStore
from .models import ChatCompletionRequest
from .router import AliasRouter, backend_tag
from .sessions import SessionRegistry, resolve_session_id

router = APIRouter()
_log = get_logger("fitt.gateway.chat")


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


# -------- the endpoint --------------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    config: Config = request.app.state.config
    memory: MemoryStore = request.app.state.memory
    session_registry: SessionRegistry = request.app.state.session_registry
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

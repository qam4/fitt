"""POST /v1/chat/completions — the main gateway endpoint.

Responsibilities:

* Validate the request shape (Pydantic).
* Reject concrete model IDs (must be aliases).
* Call the AliasRouter for actual dispatch.
* Translate upstream errors per the design.md failure-handling table.
* Return either a plain JSON response or an SSE stream, preserving the
  upstream order byte-for-byte modulo OpenAI envelope rewriting.
* Emit one structured log event per request (with latency, tokens,
  cost).
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
from .models import ChatCompletionRequest
from .router import AliasRouter, backend_tag

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

    # Reject concrete model IDs — client must use aliases.
    aliases = config.alias_names()
    if parsed.model not in aliases:
        # Heuristic: if it looks like a provider-prefixed name, we tell
        # the client they need to use an alias instead.
        concrete_shapes = (
            "/" in parsed.model
            or ":" in parsed.model
            or parsed.model.startswith(("claude-", "gpt-", "qwen"))
        )
        if concrete_shapes:
            raise ModelIdNotAlias(parsed.model, aliases)
        raise UnknownAlias(parsed.model, aliases)

    return parsed


# -------- upstream error translation ------------------------------


def _translate_upstream_error(exc: Exception) -> JSONResponse:
    """Map an exception from LiteLLM to the right HTTP response.

    The design's failure-handling table:

      * 429 / 529 (rate-limit / overload) → 503 + Retry-After
      * 4xx other                         → pass through with body
      * 5xx other                         → 502 + upstream message
    """
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    retry_after = None

    # Try to read Retry-After from a wrapped response.
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

    # Default: bad upstream.
    return JSONResponse(
        status_code=502,
        content={"error": {"type": "upstream_server_error", "message": message}},
    )


# -------- streaming helpers ---------------------------------------


async def _sse_stream(
    upstream: AsyncIterator[Any],
    *,
    on_token: Any | None = None,
) -> AsyncIterator[bytes]:
    """Forward LiteLLM streaming chunks as OpenAI SSE bytes.

    Each upstream chunk is a ``ModelResponse``-like object with
    ``.model_dump()``. On error mid-stream we emit an ``[ERROR]``
    event so the client can distinguish a clean end from a drop.
    """
    try:
        async for chunk in upstream:
            if hasattr(chunk, "model_dump"):
                payload = chunk.model_dump(exclude_none=True)
            elif isinstance(chunk, dict):
                payload = chunk
            else:
                payload = {"raw": str(chunk)}
            yield f"data: {json.dumps(payload)}\n\n".encode()
        yield b"data: [DONE]\n\n"
    except Exception as exc:
        err = {"error": {"type": "stream_failure", "message": str(exc)}}
        yield f"data: {json.dumps(err)}\n\n".encode()
        yield b"data: [ERROR]\n\n"


# -------- extracting token counts ---------------------------------


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull (input_tokens, output_tokens) from a LiteLLM response.

    Returns (0, 0) if the response doesn't include usage. This
    happens for some streamed responses; in that case the log will
    show cost=0 (acceptable — usage surfaces at end-of-stream for
    priced backends via a different hook we can add later).
    """
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
        return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    return 0, 0


# -------- the endpoint --------------------------------------------


@router.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    config: Config = request.app.state.config
    alias_router = AliasRouter(config)

    parsed = await _parse_request(request, config)
    started = time.perf_counter()

    try:
        dispatch = await alias_router.dispatch(parsed.model, parsed.to_litellm_body())
    except (UnknownAlias, NoBackendAvailable):
        # Domain errors have app-level handlers that produce the right
        # status codes and bodies. Let them bubble up.
        raise
    except Exception as exc:
        return _translate_upstream_error(exc)

    backend_header = backend_tag(dispatch.model_used)

    # ---- streaming path ------------------------------------------
    if dispatch.stream is not None:
        headers = {"X-FITT-Backend": backend_header, "X-FITT-Alias": parsed.model}
        if dispatch.fallback_used:
            headers["X-FITT-Fallback"] = "1"

        # We can't know final token counts until the stream ends, but
        # emitting at least the request log now means we always have an
        # entry even if the stream later errors.
        log_request(
            _log,
            alias=parsed.model,
            model=dispatch.model_used.model,
            backend=dispatch.model_used.backend,
            backend_actual=backend_header,
            latency_ms=int((time.perf_counter() - started) * 1000),
            input_tokens=0,
            output_tokens=0,
            cost_usd=Decimal("0"),
            status="stream_started",
            fallback=dispatch.fallback_used,
        )
        return StreamingResponse(
            _sse_stream(dispatch.stream),
            media_type="text/event-stream",
            headers=headers,
        )

    # ---- non-streaming path --------------------------------------
    response_obj = dispatch.response
    in_tok, out_tok = _extract_usage(response_obj)
    cost = estimate_cost(dispatch.model_used, in_tok, out_tok)
    latency_ms = int((time.perf_counter() - started) * 1000)

    log_request(
        _log,
        alias=parsed.model,
        model=dispatch.model_used.model,
        backend=dispatch.model_used.backend,
        backend_actual=backend_header,
        latency_ms=latency_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost,
        status="ok",
        fallback=dispatch.fallback_used,
    )

    if hasattr(response_obj, "model_dump"):
        body = response_obj.model_dump(exclude_none=True)
    elif isinstance(response_obj, dict):
        body = response_obj
    else:
        body = {"raw": str(response_obj)}

    headers = {"X-FITT-Backend": backend_header, "X-FITT-Alias": parsed.model}
    if dispatch.fallback_used:
        headers["X-FITT-Fallback"] = "1"
    return JSONResponse(content=body, headers=headers)

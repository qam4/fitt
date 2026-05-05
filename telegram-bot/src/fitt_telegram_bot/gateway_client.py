"""Async HTTP client for the FITT gateway.

One class, ``GatewayClient``, that streams chat completions and
lists aliases. Errors do not raise up to the handler - they surface
as a single string delta prefixed with ``⚠️``. That keeps the
handler's render loop uniform ("whatever deltas come in, append to
the Telegram message") without a separate error path.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

_log = logging.getLogger(__name__)

_STREAM_TIMEOUT_S = 120.0


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        *,
        timeout: float = _STREAM_TIMEOUT_S,
        enable_tools: bool = True,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout
        # Opt into the gateway's tool-forwarding loop by sending
        # `tool_choice: "auto"` on every chat call. The gateway
        # treats "tool_choice present" as the signal to append its
        # registered tools and run the tool-execution loop. Set to
        # False only for tests or for a debugging pass that wants
        # to bypass tools entirely.
        self._enable_tools = enable_tools

    # ---------- public API ----------------------------------------

    async def list_aliases(self) -> list[str]:
        """GET /v1/models (no auth needed but send token anyway)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r = await client.get(f"{self._base}/v1/models", headers=self._headers)
                r.raise_for_status()
            except httpx.HTTPError as e:
                _log.warning("gateway.list_aliases.failed", extra={"error": str(e)})
                return []
        data = r.json().get("data", [])
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        alias: str,
        session_id: str,
    ) -> AsyncIterator[str]:
        """Stream content deltas from the gateway.

        Yields successive chunks of assistant text. On error, yields
        a single ``⚠️`` message and stops.
        """
        body: dict[str, Any] = {
            "model": alias,
            "messages": messages,
            "stream": True,
        }
        if self._enable_tools:
            # Opt into the gateway's tool-forwarding loop. The
            # gateway will force non-streaming on this request
            # and wrap the final answer in a one-shot SSE frame,
            # so the bot's existing streaming-consumer code keeps
            # working.
            body["tool_choice"] = "auto"
        headers = {**self._headers, "X-FITT-Session": session_id}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    f"{self._base}/v1/chat/completions",
                    headers=headers,
                    json=body,
                ) as response:
                    if response.status_code >= 400:
                        payload = await response.aread()
                        async for msg in self._format_error(response, payload):
                            yield msg
                        return
                    async for delta in self._parse_sse(response):
                        yield delta
            except httpx.RequestError as e:
                yield f"⚠️ gateway unreachable: {e}"

    # ---------- internals -----------------------------------------

    async def _parse_sse(self, response: httpx.Response) -> AsyncIterator[str]:
        async for line in response.aiter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: ") :].strip()
            if payload == "[DONE]":
                return
            if payload == "[ERROR]":
                yield "⚠️ upstream stream aborted"
                return
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = _extract_delta(event)
            if delta:
                yield delta

    async def _format_error(self, response: httpx.Response, body: bytes) -> AsyncIterator[str]:
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            message = (
                parsed.get("error", {}).get("message") if isinstance(parsed, dict) else None
            ) or body.decode("utf-8", errors="replace")[:200]
        except json.JSONDecodeError:
            message = body.decode("utf-8", errors="replace")[:200]

        retry_after = response.headers.get("retry-after")
        if response.status_code == 503 and retry_after:
            yield f"⚠️ rate limited, retry in {retry_after}s"
        elif response.status_code == 401:
            yield "⚠️ gateway refused our Bearer token (401). Check secrets.yaml."
        else:
            yield f"⚠️ gateway error ({response.status_code}): {message}"


def _extract_delta(event: dict[str, Any]) -> str:
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""

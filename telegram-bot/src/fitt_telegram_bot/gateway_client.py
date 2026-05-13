"""Async HTTP client for the FITT gateway.

One class, ``GatewayClient``, that streams chat completions and
lists aliases. Errors do not raise up to the handler - they surface
as a single string delta prefixed with ``⚠️``. That keeps the
handler's render loop uniform ("whatever deltas come in, append to
the Telegram message") without a separate error path.

Every error that turns into a user-visible ⚠️ also lands in
``telegram-bot.log`` as a structured ``gateway.chat.failed``
event. The bot used to swallow these silently — an operator
seeing "gateway unreachable" in Telegram had no log to grep,
and no way to tell DNS-failure from connection-reset from
HTTP-401 without re-running the request. The structured log
entry carries the response status (when present), the parsed
``error.type`` from the gateway body, and the exception class
+ truncated detail for transport failures, so an operator can
correlate user-visible warnings with what actually happened.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

_log = structlog.get_logger("fitt.telegram_bot.gateway_client")

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
            # Identify this client to the gateway. The gateway uses
            # this for approval routing (so its Telegram-bound
            # prompts reach this bot's poller) and per-client tool
            # policies. Sending it unconditionally means the bot
            # works even when the operator forgot to tag the token
            # in secrets.yaml — the single biggest footgun during
            # Phase 4 bring-up before this header existed.
            "X-FITT-Client": "telegram",
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
                _log.warning(
                    "gateway.list_aliases.failed",
                    error_class=type(e).__name__,
                    error=str(e),
                )
                return []
        data = r.json().get("data", [])
        return [m["id"] for m in data if isinstance(m, dict) and "id" in m]

    async def list_pending_approvals(self, client: str | None = None) -> list[dict[str, Any]]:
        """GET /v1/approvals/pending[?client=...].

        Returns the ``pending`` array from the response, or an empty
        list on any error. The approval poller calls this every
        second, so we swallow transient failures rather than
        bubbling them up — a logged warning is enough.
        """
        params: dict[str, str] = {}
        if client is not None:
            params["client"] = client
        async with httpx.AsyncClient(timeout=10.0) as http:
            try:
                r = await http.get(
                    f"{self._base}/v1/approvals/pending",
                    headers=self._headers,
                    params=params,
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                _log.warning(
                    "gateway.list_pending_approvals.failed",
                    error_class=type(e).__name__,
                    error=str(e),
                )
                return []
        pending = r.json().get("pending", [])
        if not isinstance(pending, list):
            return []
        return pending

    async def list_events(
        self,
        *,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """GET /v1/events[?since=...][&limit=...].

        Returns the ``entries`` array from the response, newest
        last (file order), or an empty list on any error. The
        event pusher calls this every second; same
        transient-failure posture as ``list_pending_approvals``.

        Response shape (Phase 4.8c):
        ``{"entries": [...], "next_since": <float>|null}``.
        We use ``since`` as an exclusive cursor: pass back the
        last-seen ``ts`` and the gateway drops entries with
        ``ts <= since``. ``next_since`` is available but the
        pusher tracks its own cursor so we don't currently
        read it.
        """
        params: dict[str, str] = {"limit": str(limit)}
        if since is not None:
            params["since"] = str(since)
        async with httpx.AsyncClient(timeout=10.0) as http:
            try:
                r = await http.get(
                    f"{self._base}/v1/events",
                    headers=self._headers,
                    params=params,
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                _log.warning(
                    "gateway.list_events.failed",
                    error_class=type(e).__name__,
                    error=str(e),
                )
                return []
        entries = r.json().get("entries", [])
        if not isinstance(entries, list):
            return []
        return entries

    async def decide_approval(self, approval_id: str, decision: str) -> tuple[bool, str | None]:
        """POST /v1/approvals/{id}/decide with {decision: ...}.

        Returns ``(ok, error_detail)``:
        - ``(True, None)`` on 2xx where the gateway resolved the
          future.
        - ``(False, detail)`` on 404/403/etc. ``detail`` is a short
          human-readable reason suitable for surfacing to the user.
        """
        async with httpx.AsyncClient(timeout=10.0) as http:
            try:
                r = await http.post(
                    f"{self._base}/v1/approvals/{approval_id}/decide",
                    headers=self._headers,
                    json={"decision": decision},
                )
            except httpx.HTTPError as e:
                _log.warning(
                    "gateway.decide_approval.transport_failed",
                    approval_id=approval_id,
                    error_class=type(e).__name__,
                    error=str(e),
                )
                return False, f"transport error: {e}"
        if r.status_code // 100 == 2:
            payload = r.json() if r.content else {}
            return bool(payload.get("resolved", True)), None
        # Surface the detail for the user.
        try:
            detail = r.json().get("detail", "")
        except ValueError:
            detail = r.text
        _log.info(
            "gateway.decide_approval.failed",
            approval_id=approval_id,
            status=r.status_code,
            detail=detail,
        )
        return False, f"HTTP {r.status_code}: {detail}"

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
                        async for msg in self._format_error(
                            response,
                            payload,
                            alias=alias,
                            session_id=session_id,
                        ):
                            yield msg
                        return
                    async for delta in self._parse_sse(
                        response, alias=alias, session_id=session_id
                    ):
                        yield delta
            except httpx.RequestError as e:
                # Transport-level failure — DNS, connect refused,
                # read timeout. The bot used to swallow these
                # silently; now we record one row per failed
                # turn so the operator can grep
                # ``telegram-bot.log`` for "why did the user see
                # 'gateway unreachable'?" and find the actual
                # exception class + message. This is the bot
                # half of the gateway's
                # ``no_backend_available`` event.
                _log.warning(
                    "gateway.chat.failed",
                    alias=alias,
                    session_id=session_id,
                    failure_kind="transport",
                    error_class=type(e).__name__,
                    error=str(e)[:500],
                )
                yield f"⚠️ gateway unreachable: {e}"

    # ---------- internals -----------------------------------------

    async def _parse_sse(
        self,
        response: httpx.Response,
        *,
        alias: str,
        session_id: str,
    ) -> AsyncIterator[str]:
        async for line in response.aiter_lines():
            if not line or not line.startswith("data: "):
                continue
            payload = line[len("data: ") :].strip()
            if payload == "[DONE]":
                return
            if payload == "[ERROR]":
                # Mid-stream upstream abort — the gateway side
                # logs ``status=stream_failure``; we mirror that
                # here so a single grep across both files lines
                # up the user-visible warning with the gateway
                # event.
                _log.warning(
                    "gateway.chat.failed",
                    alias=alias,
                    session_id=session_id,
                    failure_kind="stream_aborted",
                )
                yield "⚠️ upstream stream aborted"
                return
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            delta = _extract_delta(event)
            if delta:
                yield delta

    async def _format_error(
        self,
        response: httpx.Response,
        body: bytes,
        *,
        alias: str,
        session_id: str,
    ) -> AsyncIterator[str]:
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            err_obj = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            message = (
                err_obj.get("message") if isinstance(err_obj, dict) else None
            ) or body.decode("utf-8", errors="replace")[:200]
            error_type = err_obj.get("type") if isinstance(err_obj, dict) else None
        except json.JSONDecodeError:
            message = body.decode("utf-8", errors="replace")[:200]
            error_type = None

        retry_after = response.headers.get("retry-after")
        # One log per ⚠️ yield. Carries the gateway's parsed
        # ``error.type`` so the bot's failure stream can be
        # joined with the gateway's ``chat.completion`` event
        # by ``upstream_status``+``error_type`` rather than
        # only by timestamp.
        _log.warning(
            "gateway.chat.failed",
            alias=alias,
            session_id=session_id,
            failure_kind="http_error",
            upstream_status=response.status_code,
            error_type=error_type,
            error_detail=message[:500] if isinstance(message, str) else str(message)[:500],
            retry_after=retry_after,
        )
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

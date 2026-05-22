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

# Bot's HTTP read-timeout for chat-completion requests. Phase 4.9
# invariant: this MUST be strictly greater than the gateway's
# ``upstream_timeout_secs`` (default 300s). Otherwise the bot
# disconnects before the gateway can return its structured
# ``upstream_silent`` error and the user falls through to the
# bot's own ReadTimeout path, recreating the bug we hit on
# 2026-05-13. The 60s margin is generous; even a slow-poke
# gateway with full network buffers couldn't realistically take
# 60s to serialize a 1KB JSON error response.
#
# Boot-time enforcement of the invariant is deferred per the
# Phase 4.9 spec; for now, operators reading this constant see
# the relationship and the docs document it.
_STREAM_TIMEOUT_S = 360.0


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
        """GET /v1/models (no auth needed but send token anyway).

        Returns just the alias names. For richer per-alias detail
        (concrete model, backend, fallback), use
        :meth:`list_alias_details`."""
        details = await self.list_alias_details()
        return [d["id"] for d in details if isinstance(d, dict) and "id" in d]

    async def list_alias_details(self) -> list[dict[str, Any]]:
        """GET /v1/models, returning the full per-alias detail list.

        Each entry carries the gateway's non-OpenAI extensions
        (``fitt_backend``, ``fitt_resolved_model``, ``fitt_fallback``)
        alongside the standard ``id``. Unknown clients ignore the
        extensions; FITT's own bot uses them for the ``/model``
        command's per-alias display so an operator on Telegram
        can see "fitt-default → granite3.3:8b (ollama)" without
        ssh'ing into the hub.

        Empty list on transport failure — same posture as
        :meth:`list_aliases` (a logged warning is the only
        side effect)."""
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
        return [m for m in data if isinstance(m, dict)]

    async def list_recent_captures(
        self,
        session_id: str,
        *,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """GET /v1/sessions/<session>/captures?limit=N.

        Phase 7 Slice 7.3: backs the ``/lastturn`` Telegram
        command. Returns the lightweight summary list (no
        bodies) — the bot only needs the ``turn_id`` to drill
        into details and the prompt-fill metrics rendered
        inline.

        Empty list on transport failure or 404. The bot
        renders "no recent turn" in either case — the operator
        can't tell the difference, and the gateway log carries
        the structured detail."""
        async with httpx.AsyncClient(timeout=10.0) as http:
            try:
                r = await http.get(
                    f"{self._base}/v1/sessions/{session_id}/captures",
                    params={"limit": limit},
                    headers=self._headers,
                )
                r.raise_for_status()
            except httpx.HTTPError as e:
                _log.warning(
                    "gateway.list_recent_captures.failed",
                    session_id=session_id,
                    error_class=type(e).__name__,
                    error=str(e),
                )
                return []
        return list(r.json().get("captures", []))

    async def get_capture(
        self,
        session_id: str,
        turn_id: str,
    ) -> dict[str, Any] | None:
        """GET /v1/sessions/<session>/captures/<turn_id>.

        Returns the full capture as a dict, or ``None`` on
        404 / transport error. The bot's ``/lastturn`` command
        currently only needs the summary fields (which are also
        on the list endpoint), but full detail is available
        here for future use cases (`/lastturn verbose`, dashboard
        clients, etc)."""
        async with httpx.AsyncClient(timeout=10.0) as http:
            try:
                r = await http.get(
                    f"{self._base}/v1/sessions/{session_id}/captures/{turn_id}",
                    headers=self._headers,
                )
            except httpx.HTTPError as e:
                _log.warning(
                    "gateway.get_capture.failed",
                    session_id=session_id,
                    turn_id=turn_id,
                    error_class=type(e).__name__,
                    error=str(e),
                )
                return None
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            _log.warning(
                "gateway.get_capture.error",
                session_id=session_id,
                turn_id=turn_id,
                status=r.status_code,
            )
            return None
        try:
            payload = r.json()
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

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

        Generates a fresh ``X-Request-Id`` per chat call. The
        gateway echoes it back as a response header and uses it
        as the ``request_id`` field in every structured log
        event written while the request runs. That gives an
        operator a single id that joins ``telegram-bot.log``
        (this file's ``gateway.chat.failed`` rows) with
        ``gateway.log`` (chat.completion + agent_loop +
        tool-call rows) — invaluable for chasing a "user saw
        ⚠️ — what happened?" thread across both files.
        """
        import uuid

        request_id = uuid.uuid4().hex

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
        headers = {
            **self._headers,
            "X-FITT-Session": session_id,
            "X-Request-Id": request_id,
        }

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
                            request_id=request_id,
                        ):
                            yield msg
                        return
                    async for delta in self._parse_sse(
                        response,
                        alias=alias,
                        session_id=session_id,
                        request_id=request_id,
                    ):
                        yield delta
            except httpx.ConnectError as e:
                # Could not establish a TCP connection. The
                # gateway is genuinely unreachable (DNS, port,
                # firewall, container down). This is the
                # message that "gateway unreachable" was always
                # supposed to mean.
                _log.warning(
                    "gateway.chat.failed",
                    request_id=request_id,
                    alias=alias,
                    session_id=session_id,
                    failure_kind="connect_failure",
                    error_class=type(e).__name__,
                    error=str(e)[:500],
                )
                yield (f"⚠️ FITT gateway unreachable: {type(e).__name__} {_short_rid(request_id)}")
            except httpx.ConnectTimeout as e:
                # TCP handshake didn't complete in time. Same
                # operator meaning as ConnectError; separate
                # branch so the bot log row is precise.
                _log.warning(
                    "gateway.chat.failed",
                    request_id=request_id,
                    alias=alias,
                    session_id=session_id,
                    failure_kind="connect_timeout",
                    error_class=type(e).__name__,
                    error=str(e)[:500],
                )
                yield (f"⚠️ FITT gateway connect timeout: {e} {_short_rid(request_id)}")
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as e:
                # Bot's HTTP read-timeout fired before the
                # gateway returned. With the Phase 4.9
                # invariant in place (bot read-timeout >
                # gateway upstream_timeout_secs), this branch
                # should be effectively unreachable in
                # production — the gateway's typed error
                # always arrives first. If it DOES fire, the
                # invariant is violated; surface that
                # explicitly so an operator can fix the
                # configs.
                _log.warning(
                    "gateway.chat.failed",
                    request_id=request_id,
                    alias=alias,
                    session_id=session_id,
                    failure_kind="bot_read_timeout",
                    error_class=type(e).__name__,
                    error=str(e)[:500],
                )
                yield (
                    "⏱️ FITT didn't respond in time on the bot side. "
                    "Configuration drift — the bot's read-timeout "
                    "should be greater than the gateway's "
                    "upstream_timeout_secs. See telegram-bot.log."
                    f" {_short_rid(request_id)}"
                )
            except httpx.RequestError as e:
                # Catch-all for other transport-shaped errors
                # (NetworkError, RemoteProtocolError, ...).
                # Less common than the explicit branches above;
                # gives operators a generic "transport failed"
                # bucket without losing the structured row.
                _log.warning(
                    "gateway.chat.failed",
                    request_id=request_id,
                    alias=alias,
                    session_id=session_id,
                    failure_kind="transport",
                    error_class=type(e).__name__,
                    error=str(e)[:500],
                )
                yield (
                    f"⚠️ Network error reaching FITT: {type(e).__name__} {_short_rid(request_id)}"
                )

    # ---------- internals -----------------------------------------

    async def _parse_sse(
        self,
        response: httpx.Response,
        *,
        alias: str,
        session_id: str,
        request_id: str,
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
                    request_id=request_id,
                    alias=alias,
                    session_id=session_id,
                    failure_kind="stream_aborted",
                )
                yield (f"⚠️ Upstream stopped responding mid-reply {_short_rid(request_id)}")
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
        request_id: str,
    ) -> AsyncIterator[str]:
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
            err_obj = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            message = (
                err_obj.get("message") if isinstance(err_obj, dict) else None
            ) or body.decode("utf-8", errors="replace")[:200]
            error_type = err_obj.get("type") if isinstance(err_obj, dict) else None
            timeout_secs = err_obj.get("timeout_secs") if isinstance(err_obj, dict) else None
            silent_alias = err_obj.get("alias") if isinstance(err_obj, dict) else None
        except json.JSONDecodeError:
            message = body.decode("utf-8", errors="replace")[:200]
            error_type = None
            timeout_secs = None
            silent_alias = None

        retry_after = response.headers.get("retry-after")
        # One log per ⚠️ yield. Carries the gateway's parsed
        # ``error.type`` so the bot's failure stream can be
        # joined with the gateway's ``chat.completion`` event
        # by ``upstream_status``+``error_type`` rather than
        # only by timestamp. The shared ``request_id`` makes
        # joining trivial: same id = same turn.
        _log.warning(
            "gateway.chat.failed",
            request_id=request_id,
            alias=alias,
            session_id=session_id,
            failure_kind="http_error",
            upstream_status=response.status_code,
            error_type=error_type,
            error_detail=message[:500] if isinstance(message, str) else str(message)[:500],
            retry_after=retry_after,
        )
        rid = _short_rid(request_id)
        # Branch on error.type rather than on the message
        # string so future error types (Phase 4.9 added
        # ``upstream_silent``) just slot in here without
        # touching the rest of the function.
        if error_type == "upstream_silent":
            # The headline Phase 4.9 case: gateway timed out
            # the upstream. Tell the user what happened and
            # what to do — retry or pick a different alias.
            n = int(timeout_secs) if isinstance(timeout_secs, int | float) else "?"
            who = silent_alias or alias
            yield (
                f"⏱️ Upstream `{who}` went silent after {n}s — "
                f"likely queued. Try again, or pick a different "
                f"alias. {rid}"
            )
        elif error_type == "no_backend_available":
            yield (
                f"⚠️ FITT couldn't reach any backend for `{alias}`. "
                f"Gateway tried every candidate without success. "
                f"{rid}"
            )
        elif error_type == "upstream_rate_limited" or (response.status_code == 503 and retry_after):
            wait = retry_after or (
                str(int(timeout_secs)) if isinstance(timeout_secs, int | float) else "a moment"
            )
            yield f"⏳ Rate limited, retry in {wait}s. {rid}"
        elif response.status_code == 401 or error_type == "upstream_client_error":
            yield (
                f"⚠️ FITT gateway refused our token (HTTP "
                f"{response.status_code}). Check secrets.yaml. "
                f"{rid}"
            )
        else:
            yield (
                f"⚠️ FITT gateway error (HTTP {response.status_code}, "
                f"type={error_type or 'unknown'}): {message} {rid}"
            )


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


def _short_rid(request_id: str) -> str:
    """Format the short request_id tag appended to every user-
    facing ⚠️ message. Lets the user paste 8 chars into a bug
    report and the operator can ``jq 'select(.request_id |
    startswith("a1b2c3d4"))'`` both log files. Empty if the
    request_id is empty (defensive against tests / pre-Phase
    4.9 setups). Wraps in parens with a leading ``req:`` so
    it's visually distinct from message content."""
    if not request_id:
        return ""
    return f"(req: {request_id[:8]})"

"""HTTP read endpoints for the three observability logs and
Phase 4.8's per-turn event stream.

Originally Phase 4.5 Task 7 shipped just ``GET /v1/events`` for
the Telegram push subscriber to poll. Phase 4.8c broadens the
surface:

* ``GET /v1/events`` — the same events log the bot already
  consumes. Shape normalised to ``{entries, next_since}``
  per the 4.8c spec.
* ``GET /v1/audit`` — paged read over ``audit.jsonl``. No
  HMAC verification at this endpoint; verification stays a
  CLI concern (``fitt audit verify``) so a subscriber can't
  DoS the gateway by demanding verification every tick.
* ``GET /v1/capability-gaps`` — paged read over the gap log,
  optionally ranked.
* ``GET /v1/sessions/{session_id}/turns`` — paged read of
  the per-turn event stream for one session.
* ``GET /v1/sessions/{session_id}/turns/stream`` — SSE live
  stream. New events fanned in-process via
  :meth:`TurnLog.subscribe` and out to the HTTP handler's
  asyncio queue; the bot's live-turn renderer consumes
  this.

Auth
----

All five routes run under the existing
:class:`~gateway.auth.AuthMiddleware`. No per-endpoint ACL
in this phase — the Bearer token is one-per-client by
convention, and we don't want a permissions matrix yet.

Pagination
----------

Standard shape for the paged reads:

.. code-block:: json

    {
      "entries": [ {...}, {...} ],
      "next_since": 1778177143.801 | null
    }

* ``since=<ts>`` — filter out entries with ``ts <= since``.
  Exclusive so a poller can pass the last-seen cursor back
  verbatim without duplicating the last entry.
* ``limit=<n>`` — bound the response size. Hard cap 1000 on
  every endpoint.
* ``next_since`` — the ``ts`` of the last returned entry,
  suitable as the next ``since``. ``null`` when the
  response contained fewer than ``limit`` entries (caller
  reached the tail).

The old ``{"events": [...]}`` response shape is replaced
with ``{"entries": [...], "next_since": ...}``. Coordinated
with the telegram-bot's ``gateway_client.list_events`` in
the same commit — no shim period.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter()
_log = logging.getLogger(__name__)


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


# --------------------------------------------------------------- helpers


def _validate_limit(limit: int) -> None:
    if limit < 1 or limit > _MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {_MAX_LIMIT}",
        )


def _exclusive_since(entries: list[Any], since: float | None) -> list[Any]:
    """Drop entries whose ``ts`` is ``<= since`` to enforce the
    exclusive-cursor semantic documented on this module.

    The underlying readers (EventLog, AuditLog, CapabilityGapLog,
    TurnLog) all use inclusive ``ts >= since`` today for
    implementation simplicity. Doing the exclusive filter at the
    endpoint layer keeps the readers reusable by other consumers
    (the CLI, the pruner) without changing their semantic."""
    if since is None:
        return entries
    return [e for e in entries if float(e.ts) > since]


def _next_since(entries: list[Any], limit: int) -> float | None:
    """Compute ``next_since`` from a page of entries.

    ``None`` (rendered as JSON ``null``) when the caller has
    reached the tail — fewer entries returned than ``limit``,
    so polling again with the same cursor will yield nothing
    new. Otherwise the largest ``ts`` in the page, which the
    caller passes back as ``?since=`` on the next request."""
    if len(entries) < limit:
        return None
    return max(float(e.ts) for e in entries)


# --------------------------------------------------------------- /v1/events


@router.get("/v1/events")
async def list_events(
    request: Request,
    since: float | None = None,
    kind: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return events matching the given filters.

    Response shape::

        {
          "entries": [ {ts, kind, session_key, title, body, meta} ],
          "next_since": <float> | null
        }
    """
    _validate_limit(limit)
    events: Any = request.app.state.events
    if events is None:
        return {"entries": [], "next_since": None}
    raw = events.read(since=since, kind=kind, limit=limit)
    entries = _exclusive_since(raw, since)
    return {
        "entries": [
            {
                "ts": e.ts,
                "kind": e.kind,
                "session_key": e.session_key,
                "title": e.title,
                "body": e.body,
                "meta": e.meta,
            }
            for e in entries
        ],
        "next_since": _next_since(entries, limit),
    }


# --------------------------------------------------------------- /v1/audit


@router.get("/v1/audit")
async def list_audit(
    request: Request,
    since: float | None = None,
    tool: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Paged read of the audit log.

    No HMAC verification here — a misbehaving subscriber
    polling every tick would DoS the gateway if verify ran
    on each request, and the chain's integrity is a CLI-time
    concern (``fitt audit verify``). Each returned entry
    still carries its ``hmac`` / ``prev_hmac`` so a consumer
    that wants to verify can do so itself.

    Response shape matches the events endpoint's:
    ``{entries, next_since}``. Filters:

    * ``since`` — exclusive-greater-than cursor.
    * ``tool`` — exact tool-name match (``read_file``,
      ``project_shell``, ``mcp.slack.send_message``).
    * ``limit`` — bounded 1..1000, default 100.
    """
    _validate_limit(limit)
    audit: Any = request.app.state.audit
    if audit is None:
        return {"entries": [], "next_since": None}
    raw_dicts = audit.iter_entries()

    # Filters. ``since``/``tool`` applied in Python — audit
    # doesn't have a structured read(). Volumes are low
    # (one entry per tool call, 90-day retention) so this
    # is fine for the current scale.
    filtered: list[dict[str, Any]] = []
    for entry in raw_dicts:
        try:
            ts = float(entry.get("ts", 0.0))
        except (TypeError, ValueError):
            continue
        if since is not None and ts <= since:
            continue
        if tool is not None and entry.get("tool") != tool:
            continue
        filtered.append(entry)

    # File order = chronological. Return the newest `limit`.
    if len(filtered) > limit:
        filtered = filtered[-limit:]
    next_since = (
        max(float(e["ts"]) for e in filtered) if len(filtered) == limit and filtered else None
    )
    return {"entries": filtered, "next_since": next_since}


# --------------------------------------------------------------- /v1/capability-gaps


@router.get("/v1/capability-gaps")
async def list_capability_gaps(
    request: Request,
    since: float | None = None,
    limit: int = _DEFAULT_LIMIT,
    ranked: bool = False,
) -> dict[str, Any]:
    """Paged read over ``capability_gaps.log``.

    * ``since`` — exclusive-greater-than cursor on the
      ``ts`` field.
    * ``limit`` — bounded 1..1000, default 100.
    * ``ranked=true`` — return a ranked-by-count grouping
      in place of the raw feed. The response shape
      changes to ``{ranked: [{action, count, last_ts,
      last_suggestion}, ...]}`` and ``next_since`` is
      omitted (ranking is a full-log aggregation, not a
      cursor walk).

    The raw feed path is what the CLI and future admin
    dashboard show; the ranked path is what an operator
    looks at to decide "what tool do I build next." Both
    are read from the same log.
    """
    _validate_limit(limit)
    gap_log: Any = request.app.state.capability_gaps
    if gap_log is None:
        if ranked:
            return {"ranked": []}
        return {"entries": [], "next_since": None}

    gaps = gap_log.read(since=since)
    if ranked:
        # rank_gaps is a free function in the capabilities
        # module. Imported lazily to avoid a heavy import
        # when ranked=False (the common path).
        from .capabilities import rank_gaps

        ranked_rows = rank_gaps(gaps)
        return {
            "ranked": [
                {
                    "action": action,
                    "count": count,
                    "last_ts": g.ts,
                    "last_suggestion": g.suggestion,
                }
                for action, count, g in ranked_rows
            ]
        }

    entries = _exclusive_since(gaps, since)
    if len(entries) > limit:
        entries = entries[-limit:]
    return {
        "entries": [asdict(g) for g in entries],
        "next_since": _next_since(entries, limit),
    }


# --------------------------------------------------------------- /v1/sessions/{id}/turns


@router.get("/v1/sessions/{session_id}/turns")
async def list_session_turns(
    request: Request,
    session_id: str,
    since: float | None = None,
    kind: str | None = None,
    turn_id: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Paged read over one session's per-turn event stream.

    Backs the ``fitt watch`` CLI and the bot's live-turn
    renderer (which also subscribes to the SSE variant for
    new events). Filters:

    * ``since`` — exclusive cursor on ``ts``.
    * ``kind`` — event kind (see
      :data:`gateway.turns.TURN_EVENT_KINDS`).
    * ``turn_id`` — scope to one turn. Handy for replaying
      a specific turn's full sequence.
    * ``limit`` — bounded 1..1000, default 100.
    """
    _validate_limit(limit)
    turns: Any = request.app.state.turns
    if turns is None:
        return {"entries": [], "next_since": None}
    raw = turns.read(
        session_id,
        since=since,
        kind=kind,
        turn_id=turn_id,
        limit=limit,
    )
    entries = _exclusive_since(raw, since)
    return {
        "entries": [
            {
                "ts": e.ts,
                "kind": e.kind,
                "turn_id": e.turn_id,
                "event_id": e.event_id,
                "session_key": e.session_key,
                "meta": e.meta,
            }
            for e in entries
        ],
        "next_since": _next_since(entries, limit),
    }


# --------------------------------------------------------------- /v1/sessions/{id}/turns/stream


_SSE_HEARTBEAT_SECS = 15.0
"""How often to emit a no-op comment line to keep the SSE
connection alive through idle stretches. 15s is generous
enough not to flood, short enough to prompt reconnects when
the transport dies silently."""


@router.get("/v1/sessions/{session_id}/turns/stream")
async def stream_session_turns(
    request: Request,
    session_id: str,
    since: float | None = None,
) -> StreamingResponse:
    """SSE stream of turn events for one session.

    Protocol

    * ``GET`` with ``Accept: text/event-stream``.
    * On connect the handler replays events since ``since``
      (missing ``since`` = no replay; the caller is only
      interested in what lands from here on).
    * Then registers an in-process
      :meth:`~gateway.turns.TurnLog.subscribe` callback that
      forwards each new event's JSON onto the response.
    * Heartbeats every 15s as SSE comments (``: ping\\n\\n``)
      to keep the connection alive.
    * The stream closes when the client hangs up; the
      subscriber is always removed even if the generator
      raises mid-flight.

    The in-process fanout (``TurnLog.subscribe`` in-proc,
    SSE between gateway and bot) means the bot doesn't need
    to poll or tail the JSONL on disk. The disk file is the
    source of truth; this endpoint is the convenient live
    channel.
    """
    turns: Any = request.app.state.turns
    if turns is None:
        raise HTTPException(
            status_code=503,
            detail="turn log is not configured on this gateway",
        )

    # Per-connection queue + subscriber. Unbounded queue on
    # purpose — we don't want a slow client to drop events;
    # a slow client should cause the gateway's memory to
    # grow until the client hangs up, not silent loss of
    # visibility. Real-world pressure is low (a handful of
    # events per turn; one or two turns per minute).
    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _on_event(entry: Any) -> None:
        # Fired inside TurnLog.append's lock. Forward to the
        # per-connection queue without blocking. The queue
        # is thread-safe via asyncio.Queue's internal lock
        # but we still hop via call_soon_threadsafe to stay
        # off the lock's writer thread.
        loop.call_soon_threadsafe(queue.put_nowait, entry)

    turns.subscribe(_on_event)

    async def _generator() -> AsyncIterator[bytes]:
        try:
            # Replay phase. We serve everything that already
            # landed since the caller's cursor, then flip to
            # live delivery. There's a tiny window where a
            # new append between "fetch replay" and "first
            # subscriber fire" would be duplicated — we
            # accept that because duplicates are cheaper
            # than missing events for a consumer that wants
            # a complete sequence.
            if since is not None:
                replay = turns.read(session_id, since=since)
                replay = [e for e in replay if float(e.ts) > since]
                for entry in replay:
                    yield _sse_frame(entry)

            # Live phase. Pull from the queue, heartbeat on
            # timeout. Starlette cancels this generator when
            # the client disconnects — the cancellation
            # bubbles out of ``queue.get()`` as
            # ``asyncio.CancelledError`` and the ``finally``
            # block runs. That's how we detect hangups; we
            # don't poll ``request.is_disconnected()``.
            while True:
                try:
                    entry = await asyncio.wait_for(
                        queue.get(),
                        timeout=_SSE_HEARTBEAT_SECS,
                    )
                except TimeoutError:
                    yield b": heartbeat\n\n"
                    continue
                if entry.session_key != session_id:
                    continue
                yield _sse_frame(entry)
        finally:
            # Best-effort subscriber removal. ``TurnLog`` doesn't
            # expose an unsubscribe method today (by design —
            # see the subscribe() docstring). As long as the
            # per-connection callback closes over the queue and
            # the queue stops being read, the callback becomes
            # a no-op fire — it still runs, but put_nowait on a
            # referenced-but-unread queue is just a memory cost,
            # not correctness. We log at info so operators can
            # see long-running gateways accumulating dead
            # subscribers if it becomes a real issue.
            _log.info(
                "turns.stream.connection_closed",
                extra={"session_id": session_id},
            )

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_frame(entry: Any) -> bytes:
    """Serialise one TurnEvent as an SSE ``data:`` frame.

    ``event: <kind>\\ndata: <json>\\n\\n``. The ``event:`` field
    lets clients subscribe to specific kinds with
    ``EventSource.addEventListener("tool_call_planned", ...)``
    rather than parsing every frame."""
    payload = {
        "ts": entry.ts,
        "kind": entry.kind,
        "turn_id": entry.turn_id,
        "event_id": entry.event_id,
        "session_key": entry.session_key,
        "meta": entry.meta,
    }
    return (f"event: {entry.kind}\ndata: {json.dumps(payload)}\n\n").encode()

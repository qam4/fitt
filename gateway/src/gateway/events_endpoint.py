"""HTTP endpoint for reading the event log.

Task 7a/b of Phase 4.5: the Telegram bot (and future push
subscribers) fetch new events by polling
``GET /v1/events?since=<ts>``. The gateway is the source of truth;
subscribers are independent processes that poll and deliver.

Why polling instead of the spec's "EventLog.append fires the
pusher":

The gateway and bot run as separate processes (Phase 3.5's Docker
layout). An in-process append-hook would work only if the pusher
lived inside the gateway — which means the gateway would have to
embed a Telegram API client, bot token, and allowlist. That's
"reinvent half the bot in the gateway." The :class:`ApprovalPoller`
in the bot already demonstrates the polling pattern works for the
approval flow; this is the same shape for async events.

Shape:

* ``GET /v1/events`` — list events newer than ``?since=<ts>``
  (unix seconds). Optional ``?kind=<k>`` filter. Bounded by
  ``?limit=<n>`` (default 100, hard cap 1000) so a long-running
  bot that fell behind doesn't blow its own memory or the
  gateway's.

Auth reuses :class:`~gateway.auth.AuthMiddleware`: standard Bearer
token. No per-event client-tag filtering today — events don't
carry a target client (unlike approvals), and in the single-user
setup there's exactly one consumer. Per-client routing is a
future extension if multiple push surfaces ever compete.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


@router.get("/v1/events")
async def list_events(
    request: Request,
    since: float | None = None,
    kind: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return events newer than ``since`` (unix seconds).

    ``since=None`` returns the latest ``limit`` events — handy
    for first-boot "where are we" queries. Typical subscriber
    flow: read the latest ts once on startup, then poll with
    ``since=<last_seen>`` thereafter.

    Response shape:

    .. code-block:: json

        {
            "events": [
                {
                    "ts": 1778177115.84,
                    "kind": "cron_fired",
                    "session_key": "cron:abc:1778177115",
                    "title": "cron 'Lunch reminder'",
                    "body": "",
                    "meta": {"cron_id": "abc", "alias": "fitt-default"}
                }
            ]
        }
    """
    if limit < 1 or limit > _MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must be between 1 and {_MAX_LIMIT}",
        )

    events: Any = request.app.state.events
    if events is None:
        # No event log configured — treat as empty rather than
        # erroring. Test-only path in practice; production
        # gateways always wire this.
        return {"events": []}

    entries = events.read(since=since, kind=kind, limit=limit)
    return {
        "events": [
            {
                "ts": e.ts,
                "kind": e.kind,
                "session_key": e.session_key,
                "title": e.title,
                "body": e.body,
                "meta": e.meta,
            }
            for e in entries
        ]
    }

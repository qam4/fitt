"""HTTP endpoints for per-turn captures (Phase 7 Slice 7.2).

Two endpoints over the per-turn JSON sidecars written by
:mod:`gateway.turn_capture`:

* ``GET /v1/sessions/<session>/captures?limit=N&since=<ts>`` —
  lightweight summary list. Used by the dashboard's turn list
  and by ``fitt turn list``. Bodies (dispatched_messages,
  response, tool_calls) are dropped — listing 50 turns
  shouldn't load megabytes of payload.
* ``GET /v1/sessions/<session>/captures/<turn_id>`` — full
  capture for one turn. Used by the dashboard's turn detail
  view, ``/lastturn`` Telegram command, and ``fitt turn show``.

Path collision note: Phase 4.8c already serves
``GET /v1/sessions/<id>/turns`` for the **per-event** stream
(turn lifecycle events from :mod:`gateway.turns`). The
captures here are a sibling concept — same per-turn id space,
different on-disk shape (sidecar JSON vs JSONL events). Two
distinct paths so the dashboard / CLI can hit each
independently. The naming follows the on-disk shape:
``turns`` for the event stream, ``captures`` for the body
sidecars.

Bearer auth via the existing middleware. Read-only.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/v1/sessions/{session_key}/captures")
async def list_captures(
    request: Request,
    session_key: str,
    limit: int = 50,
    since: float | None = None,
) -> dict[str, Any]:
    """Return recent captured turns for the session, newest first.

    Lightweight: bodies elided, just the summary fields the
    dashboard's list view renders.

    Returns empty list for a session that has no captures (new
    session, traceability off, all turns from coding-agent).
    """
    store = getattr(request.app.state, "turn_capture", None)
    if store is None:
        return {"session_key": session_key, "captures": []}
    items = store.list_recent(session_key, limit=max(1, min(limit, 500)), since=since)
    return {"session_key": session_key, "captures": items}


@router.get("/v1/sessions/{session_key}/captures/{turn_id}")
async def get_capture(
    request: Request,
    session_key: str,
    turn_id: str,
) -> dict[str, Any]:
    """Return the full capture for one turn, or 404."""
    store = getattr(request.app.state, "turn_capture", None)
    if store is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": "turn capture not available",
                    "detail": "traceability disabled or store unavailable",
                }
            },
        )
    cap = store.read(session_key, turn_id)
    if cap is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "not_found",
                    "message": f"capture for turn {turn_id!r} not found in session {session_key!r}",
                    "detail": (
                        "the turn may not exist, capture may have been "
                        "disabled for the originating client (router-mode "
                        "/ coding-agent), or the file may have been pruned."
                    ),
                }
            },
        )
    payload: dict[str, Any] = cap.to_dict()
    return payload

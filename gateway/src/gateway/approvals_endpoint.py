"""HTTP endpoints for the approval UI to poll and decide.

Exposes two endpoints:

* ``GET  /v1/approvals/pending`` — list pending approvals,
  optionally filtered by ``?client=telegram`` so a poller only
  sees approvals routed to its UI.
* ``POST /v1/approvals/{id}/decide`` — resolve a pending approval
  with ``{decision: approve | reject | trust_session}``.

Auth is the standard Bearer-token check applied by
:class:`~gateway.auth.AuthMiddleware`. The decide handler
additionally requires the requesting token's client tag to match
the approval's target client (so the IDE token can't approve a
prompt routed to the Telegram bot). This protects against a
compromised token for one client exposing approvals targeted at
another.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .approval import DecisionLiteral, PendingApproval

router = APIRouter()


class DecideBody(BaseModel):
    """Request body for ``POST /v1/approvals/{id}/decide``."""

    decision: DecisionLiteral


def _render(pending: PendingApproval) -> dict[str, Any]:
    """Serialise a pending approval for the list endpoint.

    Deliberately omits the ``future`` (not JSON-serialisable) and
    includes an ``age_s`` field the poller can use to show "3s
    ago" on the prompt.
    """
    return {
        "id": pending.approval_id,
        "tool": pending.tool_name,
        "args_summary": pending.args_summary,
        "client": pending.client,
        "session": pending.session_key,
        "age_s": round(pending.age_s(), 2),
    }


@router.get("/v1/approvals/pending")
async def list_pending(
    request: Request,
    client: str | None = None,
) -> dict[str, Any]:
    """List pending approvals.

    ``client`` query parameter filters to approvals routed to that
    client. The Telegram bot calls this with ``?client=telegram``
    on every poll; other UIs (future IDE plugin) would use their
    own tag.

    Returns at most ``limit`` approvals oldest-first so the
    poller surfaces older prompts before newer ones (first-in
    first-handled UX).
    """
    approval: Any = request.app.state.approval
    pending = await approval.list_pending(client=client)
    # Oldest first. Since created_at is monotonic, sort ascending.
    pending.sort(key=lambda p: p.created_at)
    return {"pending": [_render(p) for p in pending]}


@router.post("/v1/approvals/{approval_id}/decide")
async def decide(
    approval_id: str,
    body: DecideBody,
    request: Request,
) -> dict[str, Any]:
    """Resolve a pending approval.

    Returns:
        ``{"ok": true, "resolved": true}`` on success.

    Raises:
        * 404 when the id is unknown (never existed, or already
          timed out / resolved).
        * 403 when the requesting client tag doesn't match the
          approval's target client.
    """
    approval: Any = request.app.state.approval
    pending = await approval.get_pending(approval_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="approval not found or already resolved")

    # Client-tag authorisation: prevent an IDE token from
    # approving a Telegram-bound prompt. We know the requesting
    # client from the auth middleware.
    requester_client = getattr(request.state, "client", None)
    if requester_client is None:
        # Defensive: auth middleware should have populated this.
        raise HTTPException(status_code=401, detail="no client tag on request")
    if requester_client != pending.client:
        raise HTTPException(
            status_code=403,
            detail=(
                f"client {requester_client!r} cannot decide approvals "
                f"routed to client {pending.client!r}"
            ),
        )

    ok = await approval.resolve_approval(approval_id, body.decision)
    return {"ok": ok, "resolved": ok}

"""Approval pipeline for tool calls.

Every tool invocation goes through :meth:`ApprovalMiddleware.check`
before execution. The middleware resolves the tool's approval
bucket via the registry's policy ladder and returns an
:class:`~gateway.tools.ApprovalDecision`:

    * ``auto``        → execute immediately (most reads)
    * ``block``       → never execute (policy-level kill switch)
    * ``ask`` /
      ``trust_session`` →
                        create a pending approval, await the
                        user's decision via an out-of-band UI
                        (Telegram today, future: IDE native),
                        then resolve. 2-hour default timeout.
    * ``yolo``        → deferred until Task 8d ships the
                        YOLO-window state; rejected for now.

Two-process shape
-----------------

The gateway (this module) and the Telegram bot run as separate
processes. They coordinate via the gateway's HTTP surface:

    1. chat.py calls ``check(tool, args, ctx)``.
    2. For ``ask`` / ``trust_session`` we create an
       ``asyncio.Future``, stash it in ``_pending`` keyed by a
       fresh UUID, and ``await`` with a 2-hour timeout.
    3. The Telegram bot polls ``GET /v1/approvals/pending``,
       surfaces each entry as an inline-keyboard message.
    4. When the user clicks, the bot POSTs to
       ``/v1/approvals/{id}/decide`` with
       ``{decision: approve|reject|trust_session}``.
    5. The decide handler calls ``resolve_approval(id, decision)``,
       which sets the future.
    6. ``check`` wakes, maps to an ``ApprovalDecision``, returns.

State
-----

Pending approvals live in an in-memory dict. Lost on gateway
restart — if the operator reboots the gateway, any in-flight
approvals timeout and the model sees a ``timeout`` detail
("gateway restarted; retry"). Persistent approval records live in
Phase 4.5's event log, not here.

Deferred
--------

* Deny-list check before bucket resolution (Task 12).
* HMAC-chained audit entry per decision (Task 13).
* Real session-trust state (Task 8c). Today the placeholders are
  no-ops; once 8c ships, ``resolve_approval`` with
  ``trust_session`` should grant the session and future calls to
  the same tool return ``trust_session`` directly.
* YOLO-window timers (Task 8d).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from .tools import ApprovalBucket, ApprovalDecision

if TYPE_CHECKING:
    from .tools import Tool, ToolContext, ToolRegistry

_log = logging.getLogger(__name__)


_DEFAULT_APPROVAL_TIMEOUT_S = 2 * 60 * 60
"""Two hours. Long enough that a user who's stepped away for
lunch can still approve; short enough that a forgotten prompt
doesn't stall the chat loop forever."""


DecisionLiteral = Literal["approve", "reject", "trust_session"]
"""What the bot can POST to the decide endpoint."""


@dataclass
class PendingApproval:
    """One in-flight ``ask`` / ``trust_session`` prompt.

    Stored in ``ApprovalMiddleware._pending``. The future is what
    ``check()`` awaits; the decide handler resolves it.
    """

    approval_id: str
    tool_name: str
    args_summary: str
    """A short (~200 char) rendering of ``args`` for display. We
    don't store the full dict to keep pending records small and
    because long values (file contents for write_file) are
    unhelpful on a phone screen."""
    client: str
    """Which client originated the request — used to target the
    poller so a Telegram bot doesn't see approvals meant for an
    IDE-native UI."""
    session_key: str
    created_at: float
    """monotonic() timestamp. Used for age calculation and for
    the timeout sweep."""
    future: asyncio.Future[DecisionLiteral] = field(repr=False)

    def age_s(self) -> float:
        """Seconds since this approval was requested."""
        return monotonic() - self.created_at


class ApprovalMiddleware:
    """Coordinates tool-call approvals across the chat loop and
    the out-of-band approval UI (Telegram, future: IDE native).

    One instance per gateway process. Holds in-memory state for
    pending approvals; everything else (session-trust, YOLO
    windows) is still placeholder and ships with later tasks.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        approval_timeout_s: float = _DEFAULT_APPROVAL_TIMEOUT_S,
    ) -> None:
        self._registry = registry
        self._pending: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._timeout_s = approval_timeout_s

    # ------------------------------------------------ bucket dispatch

    async def check(
        self,
        tool: Tool,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ApprovalDecision:
        """Decide whether to run ``tool`` with ``args``.

        ``auto`` and ``block`` are immediate. ``ask`` and
        ``trust_session`` create a pending approval and wait for
        the bot (or other UI) to resolve it — up to
        ``approval_timeout_s``. ``yolo`` is deferred to Task 8d.
        """
        bucket = self._registry.resolve_bucket(
            tool, client=context.client, session_key=context.session_key
        )
        _log.info(
            "approval.resolved",
            extra={
                "tool": tool.name,
                "client": context.client,
                "session": context.session_key,
                "bucket": bucket.value,
            },
        )

        if bucket is ApprovalBucket.AUTO:
            return ApprovalDecision.auto()

        if bucket is ApprovalBucket.BLOCK:
            return ApprovalDecision.blocked(
                detail=(
                    f"Tool {tool.name!r} is blocked by policy for "
                    f"client {context.client!r}. Adjust "
                    f"`tools.per_client` in config.yaml if that's "
                    f"not what you intended."
                )
            )

        if bucket is ApprovalBucket.YOLO:
            # 8d not shipped; reject with a clear detail so the
            # model doesn't loop. Once 8d lands, check the per-
            # client YOLO window here and return .yolo() if active.
            return ApprovalDecision.rejected(
                detail=(
                    f"Tool {tool.name!r} is in the yolo bucket but "
                    f"YOLO windows are not yet wired (Task 8d). Fall "
                    f"back to ask or auto in config.yaml."
                )
            )

        # ASK / TRUST_SESSION — request human approval out of band.
        return await self._request_and_wait(tool, args, context, bucket)

    # ------------------------------------------------ request lifecycle

    async def _request_and_wait(
        self,
        tool: Tool,
        args: dict[str, Any],
        context: ToolContext,
        bucket: ApprovalBucket,
    ) -> ApprovalDecision:
        """Create a pending approval and block until it's resolved
        or times out. Called only for ``ask`` / ``trust_session``.
        """
        approval = await self.request_approval(tool, args, context)
        try:
            decision_str = await asyncio.wait_for(approval.future, timeout=self._timeout_s)
        except TimeoutError:
            # Remove from pending — if the bot polls after this,
            # the approval is gone (bot handles "unknown id" with
            # an edit-message that says "expired").
            async with self._lock:
                self._pending.pop(approval.approval_id, None)
            return ApprovalDecision.timeout(
                detail=(
                    f"Tool {tool.name!r} approval timed out after "
                    f"{int(self._timeout_s)}s. If this is a recurring "
                    f"tool you want to auto-approve, consider setting "
                    f"bucket=auto for the client in config.yaml."
                )
            )
        else:
            # Resolved explicitly by a call to resolve_approval.
            # _pending was cleared there.
            if decision_str == "approve":
                return ApprovalDecision.approved(detail="approved by user")
            if decision_str == "trust_session":
                # Ship a grant once 8c lands; no-op today.
                self.trust_session(context.session_key, tool.name)
                return ApprovalDecision.trust_session(detail="trusted for this session")
            # Anything else — treat as reject.
            return ApprovalDecision.rejected(detail=f"rejected by user: {decision_str!r}")

    async def request_approval(
        self,
        tool: Tool,
        args: dict[str, Any],
        context: ToolContext,
    ) -> PendingApproval:
        """Create and store a new pending approval. Returns it so
        tests (and, in principle, alternative UIs) can await the
        future directly. The chat loop calls ``check()``, which
        calls this internally.
        """
        approval_id = str(uuid.uuid4())
        future: asyncio.Future[DecisionLiteral] = asyncio.get_running_loop().create_future()
        pending = PendingApproval(
            approval_id=approval_id,
            tool_name=tool.name,
            args_summary=_summarise_args(args),
            client=context.client,
            session_key=context.session_key,
            created_at=monotonic(),
            future=future,
        )
        async with self._lock:
            self._pending[approval_id] = pending
        _log.info(
            "approval.requested",
            extra={
                "approval_id": approval_id,
                "tool": tool.name,
                "client": context.client,
                "session": context.session_key,
            },
        )
        return pending

    async def resolve_approval(
        self,
        approval_id: str,
        decision: DecisionLiteral,
    ) -> bool:
        """Resolve a pending approval's future.

        Returns ``True`` if the approval existed and was resolved
        (including idempotent calls to an already-resolved one),
        ``False`` if the id is unknown (never existed, or was
        cleared by timeout). Called by the decide HTTP handler.
        """
        async with self._lock:
            pending = self._pending.pop(approval_id, None)
        if pending is None:
            return False
        if not pending.future.done():
            pending.future.set_result(decision)
        _log.info(
            "approval.resolved_by_user",
            extra={
                "approval_id": approval_id,
                "tool": pending.tool_name,
                "client": pending.client,
                "decision": decision,
            },
        )
        return True

    async def list_pending(self, client: str | None = None) -> list[PendingApproval]:
        """Return a snapshot of pending approvals, optionally
        filtered by client tag. Used by ``GET /v1/approvals/pending``.
        """
        async with self._lock:
            if client is None:
                return list(self._pending.values())
            return [p for p in self._pending.values() if p.client == client]

    async def get_pending(self, approval_id: str) -> PendingApproval | None:
        """Return one pending approval by id, or ``None`` if not
        found. Used by the decide handler for client-tag
        authorisation before resolving.
        """
        async with self._lock:
            return self._pending.get(approval_id)

    # ------------------------------------------------ placeholders

    # Reserved for later tasks so callers don't have to
    # conditionally import different symbols as features land.

    def trust_session(self, session_key: str, tool_name: str) -> None:
        """Placeholder for Task 8c session-trust grant.

        Called after the user clicks "Trust session" on a prompt.
        Today logs but doesn't persist state — future calls to
        the same tool in the same session will still go through
        the ask path. 8c makes it real.
        """
        _log.debug(
            "approval.trust_session.noop",
            extra={"session": session_key, "tool": tool_name},
        )

    def clear_session(self, session_key: str) -> None:
        """Drop all session-level state for ``session_key``."""
        _log.debug("approval.clear_session.noop", extra={"session": session_key})


def _summarise_args(args: dict[str, Any]) -> str:
    """Render a tool-call's args for display on a phone screen.

    Truncates long values, avoids embedding secrets by deferring
    to repr() (which quotes strings — operators see what came in
    without us trying to guess what's sensitive). Cap total
    length at ~200 chars — enough for 3-4 small fields, short
    enough for a Telegram message.
    """
    if not args:
        return "(no args)"
    parts: list[str] = []
    for k, v in args.items():
        rendered = repr(v)
        if len(rendered) > 60:
            rendered = rendered[:57] + "..."
        parts.append(f"{k}={rendered}")
    summary = ", ".join(parts)
    if len(summary) > 200:
        summary = summary[:197] + "..."
    return summary

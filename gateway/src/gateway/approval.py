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

* YOLO-window timers (Task 8d).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, Literal

from .tools import ApprovalBucket, ApprovalDecision, deny_list

if TYPE_CHECKING:
    from .tools import Tool, ToolContext, ToolRegistry

_log = logging.getLogger(__name__)


_DEFAULT_APPROVAL_TIMEOUT_S = 600.0
"""10 minutes.

Long enough for a phone tap from a locked screen, short
enough that a forgotten approval doesn't sit forever.

Why not infinite: pending approvals live in an in-memory
dict and don't survive a gateway restart. Some bound aligns
disk + memory state — if the operator restarts after 12
hours, they're not in a worse place than if they hadn't.

Why not the prior 45s: 45s was sized to fit inside the bot's
HTTP client timeout (~60s) so the chat request didn't return
"gateway unreachable" while waiting on a tap. Detached
delivery (Phase 4.5 Task 5.5,
``approval_detach_threshold_secs``) decoupled this — when
detach is enabled the chat request returns a placeholder
fast and the tool work continues in the background, so
``approval_timeout_secs`` no longer needs to fit inside the
HTTP timeout. Without detach enabled, a 10-minute timeout
will still expose the HTTP client timeout (the request
returns "gateway unreachable" if the user takes longer than
~60s to tap, even though the gateway is fine and the
approval is still pending).

Recommended pairs:

* Detach enabled (best phone UX):
  ``approval_detach_threshold_secs: 30`` +
  ``approval_timeout_secs: 7200``  (2h)
* Detach disabled (today's default):
  ``approval_timeout_secs: 600``  (10 min)
  — accept the "gateway unreachable" message in the bot
  if you take more than 60s; the underlying approval is
  still valid until 10min, so a tap within that window
  resolves correctly even though the bot already
  surfaced the disconnect.

Override via ``tools.approval_timeout_secs`` in
config.yaml."""


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
        # Session-trust state: for each session_key, the set of
        # tool names the user has tapped "Trust session" on.
        # Populated by :meth:`trust_session`, consulted by
        # :meth:`check` to skip re-prompting for the same tool in
        # the same session. Cleared per-session via
        # :meth:`clear_session`; dropped entirely on gateway
        # restart (in-memory only, matching the posture of
        # ``_pending``). See the "sticky approval" entry in
        # docs/observed-issues.md for the motivation.
        self._trusted: dict[str, set[str]] = {}

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
        # Deny-list runs first. Non-overridable. Covers the case
        # where a future shell-exposing tool could receive a
        # destructive command string from the model — we block
        # before any bucket resolution, audit log, or human
        # approval. Today no inline tool implements
        # `shell_command_for`, so this is a no-op for reads and
        # the curated writes; it's infrastructure waiting for the
        # future `project_shell` + MCP shell-wrapping tools.
        if tool.shell_command_for is not None:
            command_str = tool.shell_command_for(args)
            if command_str:
                hit = deny_list.check(command_str)
                if hit is not None:
                    _log.warning(
                        "approval.denied_deny_list",
                        extra={
                            "tool": tool.name,
                            "client": context.client,
                            "session": context.session_key,
                            "pattern_label": hit.label,
                        },
                    )
                    return ApprovalDecision.denied_deny_list(
                        detail=(
                            f"Tool {tool.name!r} blocked by the deny list: "
                            f"{hit.label}. This is a hardcoded safety floor; "
                            f"changing it requires a gateway code change."
                        )
                    )

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

        # Session-trust short-circuit. If the user has previously
        # tapped "Trust session" for this tool in this session,
        # the bucket resolves to ASK / TRUST_SESSION but we skip
        # the out-of-band prompt and execute immediately. The
        # deny-list check above still ran — trust is a convenience
        # on top of policy, not a way around the safety floor.
        if self._is_trusted(context.session_key, tool.name):
            _log.info(
                "approval.trust_session.hit",
                extra={
                    "tool": tool.name,
                    "client": context.client,
                    "session": context.session_key,
                },
            )
            return ApprovalDecision.trust_session(detail="previously trusted for this session")

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
        # Phase 4.8: emit approval_requested turn event so the
        # Telegram renderer and other live consumers can show
        # the pending approval immediately.
        from .turn_events import record_approval_decided, record_approval_requested

        record_approval_requested(
            getattr(context, "turns", None),
            getattr(context, "turn_id", None),
            context.session_key,
            approval_id=approval.approval_id,
            tool_name=tool.name,
            bucket=bucket.value,
            client=context.client,
        )
        approval_started = monotonic()
        try:
            decision_str = await asyncio.wait_for(approval.future, timeout=self._timeout_s)
        except TimeoutError:
            # Remove from pending — if the bot polls after this,
            # the approval is gone (bot handles "unknown id" with
            # an edit-message that says "expired").
            async with self._lock:
                self._pending.pop(approval.approval_id, None)
            record_approval_decided(
                getattr(context, "turns", None),
                getattr(context, "turn_id", None),
                context.session_key,
                approval_id=approval.approval_id,
                decision="timeout",
                duration_ms=int((monotonic() - approval_started) * 1000),
            )
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
            duration_ms = int((monotonic() - approval_started) * 1000)
            if decision_str == "approve":
                record_approval_decided(
                    getattr(context, "turns", None),
                    getattr(context, "turn_id", None),
                    context.session_key,
                    approval_id=approval.approval_id,
                    decision="approve",
                    duration_ms=duration_ms,
                )
                return ApprovalDecision.approved(detail="approved by user")
            if decision_str == "trust_session":
                # Grant the session-level trust so future calls
                # to the same tool in this session skip the
                # prompt. Previously a no-op; see
                # trust_session() docstring for history.
                self.trust_session(context.session_key, tool.name)
                record_approval_decided(
                    getattr(context, "turns", None),
                    getattr(context, "turn_id", None),
                    context.session_key,
                    approval_id=approval.approval_id,
                    decision="trust_session",
                    duration_ms=duration_ms,
                )
                return ApprovalDecision.trust_session(detail="trusted for this session")
            # Anything else — treat as reject.
            record_approval_decided(
                getattr(context, "turns", None),
                getattr(context, "turn_id", None),
                context.session_key,
                approval_id=approval.approval_id,
                decision="reject",
                duration_ms=duration_ms,
            )
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
            args_summary=_summarise_args(args, tool_name=tool.name),
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
        """Record that ``tool_name`` is trusted for the remainder
        of ``session_key`` — future ``ask`` / ``trust_session``
        calls to the same tool in the same session skip the
        out-of-band prompt and execute immediately.

        Called by :meth:`_request_and_wait` when the user taps
        "🔓 Trust session" on a Telegram approval prompt. Before
        this landed, the method was a documented no-op and the
        button did the same thing as Approve — one invocation
        ran, the next one re-prompted. See the "🔓 Trust session
        does nothing" entry in docs/observed-issues.md for the
        history.

        Trust scope is per-``(session_key, tool.name)``. It's not
        per-arguments: trusting ``project_shell`` trusts every
        command the model subsequently sends through it (still
        subject to the deny list — that safety floor runs before
        any trust check).

        Lifetime: in-memory, same process as ``_pending``. A
        gateway restart forgets all trust grants. Deliberate: a
        restart is a fresh start, and the operator's muscle
        memory shouldn't include "I trusted X last week, so X
        is still trusted this week." Persistent trust would
        graduate to config.yaml (``bucket=auto`` for the tool +
        client pair), which is the right place for a long-lived
        policy decision."""
        self._trusted.setdefault(session_key, set()).add(tool_name)
        _log.info(
            "approval.trust_session.grant",
            extra={"session": session_key, "tool": tool_name},
        )

    def _is_trusted(self, session_key: str, tool_name: str) -> bool:
        """Internal helper used by :meth:`check`'s early return.
        Public counterpart lives in tests; the production code
        path only needs the boolean."""
        return tool_name in self._trusted.get(session_key, set())

    def clear_session(self, session_key: str) -> None:
        """Drop all session-level state for ``session_key``.

        Today that's just the session's trusted-tools set. Called
        when a session is archived or deleted via the CLI; also a
        useful test hook for resetting state between cases."""
        removed = self._trusted.pop(session_key, None)
        _log.info(
            "approval.clear_session",
            extra={
                "session": session_key,
                "trusted_tools_cleared": len(removed) if removed else 0,
            },
        )


def _summarise_args(args: dict[str, Any], *, tool_name: str | None = None) -> str:
    """Render a tool-call's args for display on a phone screen.

    Truncates long values, avoids embedding secrets by deferring
    to repr() (which quotes strings — operators see what came in
    without us trying to guess what's sensitive). Cap total
    length at ~200 chars — enough for 3-4 small fields, short
    enough for a Telegram message.

    Phase 4.7: ``project_shell`` is special-cased. Its whole
    value is the command string and the user needs the full
    thing to decide; we widen the cap to 1000 chars for this
    tool and truncate with an explicit flag past that so a
    ~10KB shell command (a prompt-injection smell) doesn't
    pass quietly. Other tools keep the 200-char cap.
    """
    if not args:
        return "(no args)"
    if tool_name == "project_shell":
        return _summarise_project_shell_args(args)
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


_PROJECT_SHELL_CMD_CAP = 1000
"""Phase 4.7: hard cap on command-string display in the approval
prompt. Widened from the generic 200-char args cap because the
command IS what the user is approving. A command longer than
this is flagged as truncated — a ~10KB shell command is
prompt-injection-smelly and shouldn't pass silently."""


def _summarise_project_shell_args(args: dict[str, Any]) -> str:
    """Custom summariser for ``project_shell``.

    Shape: ``project=<name>, command=<up to 1000 chars>``. When
    the command exceeds the cap we append a flag so the user
    sees ``(truncated; N extra chars)`` rather than a silent
    cut-off — catching a suspicious payload at review time."""
    parts: list[str] = []
    project = args.get("project")
    if project is not None:
        parts.append(f"project={project!r}")
    timeout = args.get("timeout_secs")
    if timeout is not None:
        parts.append(f"timeout_secs={timeout!r}")
    command = args.get("command", "")
    if not isinstance(command, str):
        parts.append(f"command={command!r}")
        return ", ".join(parts)
    if len(command) <= _PROJECT_SHELL_CMD_CAP:
        parts.append(f"command={command!r}")
    else:
        overflow = len(command) - _PROJECT_SHELL_CMD_CAP
        parts.append(
            f"command={command[:_PROJECT_SHELL_CMD_CAP]!r} (truncated; {overflow} extra chars)"
        )
    return ", ".join(parts)

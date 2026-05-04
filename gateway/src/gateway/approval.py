"""Approval pipeline for tool calls.

Every tool invocation goes through :meth:`ApprovalMiddleware.check`
before execution. The middleware resolves the tool's approval
bucket via the registry's policy ladder and returns an
:class:`~gateway.tools.ApprovalDecision`:

    * ``auto``        → execute immediately (most reads)
    * ``block``       → never execute (policy-level kill switch)
    * ``ask`` /
      ``trust_session`` /
      ``yolo``        → require human input; not yet implemented
                        in Phase 4 Task 8 (slim). Those tools will
                        be rejected with a clear detail message
                        until Task 9 (Telegram approval UI) lands.

Design notes
------------

* **Deferred**: deny-list integration (Task 12), HMAC-chained
  audit (Task 13), session-trust tracking hooks, and the
  Telegram-futures plumbing that turns ``ask`` into a real
  prompt. This module is the skeleton those later tasks clip
  into.

* **Default-closed** for anything that isn't ``auto`` or
  ``block``: until we have a way to actually ask the user, we
  reject rather than silently run. Prevents a future write-tool
  from being auto-approved by mistake.

* **Stateless** today. When session-trust and YOLO windows land,
  they'll need a per-session state dict on this class. Kept as
  a class rather than a module-level function so the refactor
  is localised.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .tools import ApprovalBucket, ApprovalDecision

if TYPE_CHECKING:
    from .tools import Tool, ToolContext, ToolRegistry

_log = logging.getLogger(__name__)


class ApprovalMiddleware:
    """Thin wrapper around the registry's policy ladder.

    One instance per gateway process. Stateless in this slim
    version; later tasks will add session-trust and YOLO-window
    state here.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def check(
        self,
        tool: Tool,
        args: dict[str, Any],
        context: ToolContext,
    ) -> ApprovalDecision:
        """Decide whether to run ``tool`` with ``args``.

        The decision goes into the audit log (Task 13) and drives
        dispatch. A ``rejected`` decision returns a detail
        message that the chat loop surfaces to the model so it
        knows *why* the tool couldn't run — gives the LLM a
        chance to retry a different approach rather than looping
        on the same blocked call.
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

        # ASK / TRUST_SESSION / YOLO all need human input — which
        # we don't have a UI for yet. Return a rejection with a
        # detail message so the model can see the reason instead
        # of spinning.
        return ApprovalDecision.rejected(
            detail=(
                f"Tool {tool.name!r} needs approval (bucket "
                f"{bucket.value!r}) but the approval UI is not "
                f"wired yet (Phase 4 Task 9). Reads via the "
                f"`auto` bucket work; writes are deferred."
            )
        )

    # Reserved for later tasks so callers don't have to
    # conditionally import different symbols as features land.
    # These no-ops fail safe.

    def trust_session(self, session_key: str, tool_name: str) -> None:
        """Placeholder for Task 8b session-trust grant.

        Approval middleware will call this after the user
        responds "Trust session" to an ``ask`` prompt. Today
        there's no prompt and no grant; the method exists so the
        Telegram handler in Task 9 can call it without a
        conditional import.
        """
        _log.debug("approval.trust_session.noop", extra={"session": session_key})

    def clear_session(self, session_key: str) -> None:
        """Drop all session-level state for ``session_key``."""
        _log.debug("approval.clear_session.noop", extra={"session": session_key})

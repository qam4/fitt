"""Telegram-side approval UI.

Two pieces:

* ``ApprovalPoller`` — background task that polls the gateway for
  pending approvals routed to ``client=telegram``, surfaces each
  new one as an inline-keyboard message, and remembers which ids
  it's already shown so duplicate prompts don't pile up.

* ``parse_callback_data`` / ``format_prompt`` — pure helpers the
  callback handler in ``bot.py`` uses. Kept here so tests can
  exercise them without PTB plumbing.

On-disk state: none. If the bot restarts, the poller's "already
surfaced" set clears, which means any pending-but-still-valid
approvals get re-posted once on next startup. Harmless (the
gateway resolves the same approval id either way), and much
simpler than syncing state across restarts.

The bot asks for approvals targeted at itself (``client=telegram``)
via the ``?client=`` filter on the list endpoint. Cross-client
approvals (e.g. an IDE-bound prompt) are ignored.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .gateway_client import GatewayClient

_log = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL_S = 1.0
"""How often the bot asks the gateway for pending approvals.
1s is a fine human-scale balance — approval latency is already
dominated by the user reading + clicking, so a sub-second floor
on poll interval isn't worth the traffic."""


@dataclass
class ApprovalPoller:
    """Polls the gateway and surfaces new approvals to a set of
    allowlisted Telegram users.

    Construction is cheap; :meth:`run` is the hot loop. Keeps an
    in-memory ``_surfaced`` set so re-polling a still-pending
    approval doesn't re-post it.
    """

    gateway: GatewayClient
    allowlist: frozenset[int]
    on_prompt: Callable[[int, dict[str, Any]], Awaitable[None]]
    """Called once per new pending approval, for each allowlisted
    user id. Signature ``(user_id, approval_dict)``. The concrete
    bot wires this to a function that posts a message with an
    inline keyboard.

    Passing a callback rather than a ``Bot`` instance keeps the
    poller unit-testable without python-telegram-bot."""

    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S

    _surfaced: set[str] = field(default_factory=set, init=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    async def run(self) -> None:
        """Main loop. Cancels cleanly on :meth:`stop` or
        :class:`asyncio.CancelledError`."""
        _log.info("telegram.approval_poller.started")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    # A transient gateway hiccup shouldn't kill the
                    # poller. Log and sleep the normal interval.
                    _log.warning(
                        "telegram.approval_poller.tick_failed",
                        extra={"error": str(e)},
                    )
                # Sleep with early-exit on stop.
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self.poll_interval_s,
                    )
                except TimeoutError:
                    pass
        finally:
            _log.info("telegram.approval_poller.stopped")

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------ internals

    async def _tick(self) -> None:
        pending = await self.gateway.list_pending_approvals(client="telegram")
        # Drop any ids from _surfaced that are no longer pending
        # (resolved or timed out). Keeps the set bounded over long
        # sessions.
        live_ids = {p["id"] for p in pending}
        self._surfaced &= live_ids

        for entry in pending:
            ap_id = entry["id"]
            if ap_id in self._surfaced:
                continue
            # Post to every allowlisted user. In v0 the allowlist
            # is size 1, so this is usually one iteration — but
            # the loop is trivially correct for future multi-user
            # setups.
            for user_id in self.allowlist:
                try:
                    await self.on_prompt(user_id, entry)
                except Exception as e:
                    _log.warning(
                        "telegram.approval_poller.prompt_failed",
                        extra={
                            "user_id": user_id,
                            "approval_id": ap_id,
                            "error": str(e),
                        },
                    )
            self._surfaced.add(ap_id)


# --------------------------------------------------------------- pure helpers


def format_prompt(entry: dict[str, Any]) -> str:
    """Render an approval entry for a Telegram message.

    Keeps it phone-friendly: short lines, no fenced code blocks
    (PTB's markdown is fiddly; plain text is reliable).
    """
    tool = entry.get("tool", "?")
    args = entry.get("args_summary", "")
    session = entry.get("session", "main")
    age = entry.get("age_s", 0.0)
    return (
        f"🔐 Tool approval needed\nTool: {tool}\nArgs: {args}\nSession: {session}\nAge: {age:.1f}s"
    )


def build_callback_data(decision: str, approval_id: str) -> str:
    """Callback-data format: ``<decision>:<approval_id>``.

    Telegram caps callback_data at 64 bytes. UUID4 ids are 36
    chars + ``<decision>:`` prefix is at most 15 (``trust_session:``).
    Total fits comfortably. If we ever extend decisions beyond
    these three, check the total length doesn't exceed 64.
    """
    if decision not in ("approve", "reject", "trust_session"):
        raise ValueError(f"unknown decision {decision!r}")
    data = f"{decision}:{approval_id}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError(f"callback_data too long: {len(data)} bytes")
    return data


def parse_callback_data(data: str) -> tuple[str, str]:
    """Parse ``<decision>:<approval_id>`` back into a tuple.

    Returns ``(decision, approval_id)``. Raises ``ValueError`` on
    malformed data.
    """
    if ":" not in data:
        raise ValueError(f"malformed callback data: {data!r}")
    decision, _, approval_id = data.partition(":")
    if decision not in ("approve", "reject", "trust_session"):
        raise ValueError(f"unknown decision in callback data: {decision!r}")
    if not approval_id:
        raise ValueError("empty approval id in callback data")
    return decision, approval_id

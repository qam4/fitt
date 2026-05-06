"""Tests for the telegram-bot's approval UI plumbing.

Exercises the pure helpers (callback encode/decode, prompt
formatting) and the ``ApprovalPoller`` loop with a stub
gateway client.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from fitt_telegram_bot.approval import (
    ApprovalPoller,
    build_callback_data,
    format_prompt,
    parse_callback_data,
)

# --------------------------------------------------------------- helpers


@dataclass
class StubGateway:
    """Minimal GatewayClient stand-in for poller tests."""

    pending: list[dict[str, Any]] = field(default_factory=list)
    decide_calls: list[tuple[str, str]] = field(default_factory=list)
    decide_result: tuple[bool, str | None] = (True, None)

    async def list_pending_approvals(self, client: str | None = None) -> list[dict[str, Any]]:
        # Respect the filter so tests see the filter is threaded
        # through correctly.
        if client is None:
            return list(self.pending)
        return [p for p in self.pending if p.get("client") == client]

    async def decide_approval(self, approval_id: str, decision: str) -> tuple[bool, str | None]:
        self.decide_calls.append((approval_id, decision))
        return self.decide_result


# --------------------------------------------------------------- pure helpers


def test_format_prompt_has_all_fields() -> None:
    s = format_prompt(
        {
            "id": "abc",
            "tool": "edit_file",
            "args_summary": "path='foo.py'",
            "client": "telegram",
            "session": "main",
            "age_s": 3.5,
        }
    )
    assert "edit_file" in s
    assert "path='foo.py'" in s
    assert "main" in s
    assert "3.5" in s


def test_format_prompt_tolerates_missing_fields() -> None:
    s = format_prompt({"id": "x"})
    # Falls back to ? / empty string / default session.
    assert "Tool:" in s


def test_build_and_parse_callback_roundtrip() -> None:
    data = build_callback_data("approve", "11111111-2222-3333-4444-555555555555")
    assert len(data.encode("utf-8")) <= 64
    decision, ap_id = parse_callback_data(data)
    assert decision == "approve"
    assert ap_id == "11111111-2222-3333-4444-555555555555"


def test_build_callback_rejects_unknown_decision() -> None:
    with pytest.raises(ValueError):
        build_callback_data("maybe", "abc")


def test_build_callback_rejects_too_long() -> None:
    # Force a pathological length.
    with pytest.raises(ValueError):
        build_callback_data("trust_session", "x" * 100)


def test_parse_callback_rejects_malformed() -> None:
    for bad in ["no-colon", ":empty-decision", "unknown:abc", "approve:"]:
        with pytest.raises(ValueError):
            parse_callback_data(bad)


# --------------------------------------------------------------- poller


@pytest.mark.asyncio
async def test_poller_surfaces_new_approvals_once() -> None:
    """Each new approval triggers on_prompt for each allowlisted
    user, exactly once (re-polling the same pending id is a
    no-op)."""
    gw = StubGateway(
        pending=[
            {
                "id": "a1",
                "tool": "edit_file",
                "args_summary": "path='x'",
                "client": "telegram",
                "session": "main",
                "age_s": 0.1,
            },
        ]
    )
    surfaced: list[tuple[int, str]] = []

    async def on_prompt(user_id: int, entry: dict[str, Any]) -> None:
        surfaced.append((user_id, entry["id"]))

    poller = ApprovalPoller(
        gateway=gw,  # type: ignore[arg-type]
        allowlist=frozenset({42, 99}),
        on_prompt=on_prompt,
        poll_interval_s=0.01,
    )

    task = asyncio.create_task(poller.run())
    # Let at least two ticks happen.
    await asyncio.sleep(0.05)
    poller.stop()
    await asyncio.wait_for(task, timeout=1.0)

    # Two allowlisted users x 1 approval = 2 calls, no duplicates.
    assert sorted(surfaced) == [(42, "a1"), (99, "a1")]


@pytest.mark.asyncio
async def test_poller_skips_already_surfaced() -> None:
    gw = StubGateway(
        pending=[
            {
                "id": "a1",
                "tool": "edit_file",
                "args_summary": "",
                "client": "telegram",
                "session": "main",
                "age_s": 0.1,
            },
        ]
    )
    surfaced: list[str] = []

    async def on_prompt(user_id: int, entry: dict[str, Any]) -> None:
        surfaced.append(entry["id"])

    poller = ApprovalPoller(
        gateway=gw,  # type: ignore[arg-type]
        allowlist=frozenset({42}),
        on_prompt=on_prompt,
        poll_interval_s=0.01,
    )

    task = asyncio.create_task(poller.run())
    # Three full ticks, same pending — should surface exactly once.
    await asyncio.sleep(0.05)
    poller.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert surfaced == ["a1"]


@pytest.mark.asyncio
async def test_poller_forgets_resolved_ids() -> None:
    """Once an approval disappears from pending, the poller drops
    it from its _surfaced set so a new one with the same id (very
    unlikely but structurally possible) would re-surface."""
    gw = StubGateway(
        pending=[
            {
                "id": "a1",
                "tool": "edit_file",
                "args_summary": "",
                "client": "telegram",
                "session": "main",
                "age_s": 0.1,
            },
        ]
    )
    surfaced: list[str] = []

    async def on_prompt(user_id: int, entry: dict[str, Any]) -> None:
        surfaced.append(entry["id"])

    poller = ApprovalPoller(
        gateway=gw,  # type: ignore[arg-type]
        allowlist=frozenset({42}),
        on_prompt=on_prompt,
        poll_interval_s=0.01,
    )

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.03)  # surface once

    # Simulate resolution.
    gw.pending = []
    await asyncio.sleep(0.03)  # give the poller a tick to clear

    # And back with the same id (as if a new approval happened to
    # reuse it — real UUIDs won't, but the bookkeeping shouldn't
    # assume).
    gw.pending = [
        {
            "id": "a1",
            "tool": "edit_file",
            "args_summary": "",
            "client": "telegram",
            "session": "main",
            "age_s": 0.0,
        },
    ]
    await asyncio.sleep(0.05)

    poller.stop()
    await asyncio.wait_for(task, timeout=1.0)

    # Surfaced twice: once for each distinct "appearance".
    assert surfaced == ["a1", "a1"]


@pytest.mark.asyncio
async def test_poller_survives_transient_gateway_error() -> None:
    """A gateway error in one tick must not kill the poller."""

    class FlakyGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def list_pending_approvals(self, client: str | None = None) -> list[dict[str, Any]]:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated network blip")
            return []

    gw = FlakyGateway()
    poller = ApprovalPoller(
        gateway=gw,  # type: ignore[arg-type]
        allowlist=frozenset({42}),
        on_prompt=lambda *_: _noop(),  # type: ignore[arg-type]
        poll_interval_s=0.01,
    )

    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert gw.calls >= 2


async def _noop() -> None:
    return None

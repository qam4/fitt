"""Tests for Phase 4 Task 8 (slim) + Task 9 approval middleware.

The middleware now handles:
- ``auto`` → executes.
- ``block`` → doesn't execute, blocked reason.
- ``ask`` / ``trust_session`` → creates a pending approval and
  awaits resolution (Task 9). Short timeouts drive the timeout
  path in tests.
- ``yolo`` → still rejected (Task 8d deferred).

Deeper tests (deny list, audit integration) come with
Tasks 12/13.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gateway.approval import ApprovalMiddleware, _summarise_args
from gateway.projects import ProjectRegistry
from gateway.tools import (
    ApprovalBucket,
    Tool,
    ToolContext,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
)

# --------------------------------------------------------------- helpers


async def _noop(args: dict, ctx: ToolContext) -> ToolResult:
    raise AssertionError("stub should never be executed by approval tests")


def _mk_tool(
    name: str,
    bucket: ApprovalBucket = ApprovalBucket.ASK,
    *,
    kind: str = "inline",
    shell_command_for: Any = None,
) -> Tool:
    return Tool(
        name=name,
        description=f"stub for {name}",
        schema={"type": "object", "properties": {}},
        callable=_noop,
        default_bucket=bucket,
        requires_project=False,
        kind=kind,  # type: ignore[arg-type]
        shell_command_for=shell_command_for,
    )


def _ctx(reg: ProjectRegistry, client: str = "telegram") -> ToolContext:
    return ToolContext(
        client=client,
        session_key="main",
        projects=reg,
        backend=None,
    )


@pytest.fixture
def project_registry(tmp_path: Any) -> ProjectRegistry:
    return ProjectRegistry(config_path=tmp_path / "projects.yaml")


# --------------------------------------------------------------- auto


async def test_auto_bucket_executes(project_registry: ProjectRegistry) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("read_file", ApprovalBucket.AUTO))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("read_file"),
        {},
        _ctx(project_registry),
    )
    assert decision.execute is True
    assert decision.reason == "auto"


# --------------------------------------------------------------- block


async def test_block_bucket_does_not_execute(
    project_registry: ProjectRegistry,
) -> None:
    # Policy blocks write_file for webui.
    policy = ToolPolicy.from_config({"per_client": {"webui": {"write_file": "block"}}})
    reg = ToolRegistry(policy)
    reg.register(_mk_tool("write_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("write_file"),
        {},
        _ctx(project_registry, client="webui"),
    )
    assert decision.execute is False
    assert decision.reason == "blocked"
    assert "policy" in decision.detail


# --------------------------------------------------------------- yolo (still rejected)


async def test_yolo_bucket_rejected_until_8d(
    project_registry: ProjectRegistry,
) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("run_tests", ApprovalBucket.YOLO))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("run_tests"),
        {},
        _ctx(project_registry),
    )
    assert decision.execute is False
    assert decision.reason == "rejected"
    assert "Task 8d" in decision.detail


# --------------------------------------------------------------- ask: pending / resolved


async def test_ask_creates_pending_approval_and_waits(
    project_registry: ProjectRegistry,
) -> None:
    """Check blocks awaiting resolution; another task resolves it;
    check returns approved."""
    reg = ToolRegistry()
    reg.register(_mk_tool("edit_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg, approval_timeout_s=10.0)

    async def resolver() -> None:
        # Let `check` create the pending entry, then resolve it.
        # One hop is enough because everything is in-process.
        await asyncio.sleep(0.01)
        pending = await m.list_pending()
        assert len(pending) == 1, "expected exactly one pending approval"
        ok = await m.resolve_approval(pending[0].approval_id, "approve")
        assert ok is True

    decision, _ = await asyncio.gather(
        m.check(reg.lookup("edit_file"), {"path": "foo.py"}, _ctx(project_registry)),
        resolver(),
    )
    assert decision.execute is True
    assert decision.reason == "approved"

    # Pending dict should be empty after resolution.
    assert await m.list_pending() == []


async def test_ask_reject_returns_rejected(
    project_registry: ProjectRegistry,
) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("edit_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg, approval_timeout_s=10.0)

    async def resolver() -> None:
        await asyncio.sleep(0.01)
        pending = await m.list_pending()
        await m.resolve_approval(pending[0].approval_id, "reject")

    decision, _ = await asyncio.gather(
        m.check(reg.lookup("edit_file"), {"path": "foo.py"}, _ctx(project_registry)),
        resolver(),
    )
    assert decision.execute is False
    assert decision.reason == "rejected"
    assert "rejected by user" in decision.detail


async def test_ask_trust_session_returns_trust(
    project_registry: ProjectRegistry,
) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("edit_file", ApprovalBucket.TRUST_SESSION))
    m = ApprovalMiddleware(reg, approval_timeout_s=10.0)

    async def resolver() -> None:
        await asyncio.sleep(0.01)
        pending = await m.list_pending()
        await m.resolve_approval(pending[0].approval_id, "trust_session")

    decision, _ = await asyncio.gather(
        m.check(reg.lookup("edit_file"), {"path": "foo.py"}, _ctx(project_registry)),
        resolver(),
    )
    assert decision.execute is True
    assert decision.reason == "trust_session"


async def test_ask_times_out(project_registry: ProjectRegistry) -> None:
    """No resolver → approval times out; check returns timeout."""
    reg = ToolRegistry()
    reg.register(_mk_tool("edit_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg, approval_timeout_s=0.05)

    decision = await m.check(reg.lookup("edit_file"), {"path": "foo.py"}, _ctx(project_registry))
    assert decision.execute is False
    assert decision.reason == "timeout"
    # Timed-out approval is cleaned up.
    assert await m.list_pending() == []


async def test_resolve_unknown_returns_false() -> None:
    reg = ToolRegistry()
    m = ApprovalMiddleware(reg)
    assert await m.resolve_approval("does-not-exist", "approve") is False


async def test_list_pending_filters_by_client(
    project_registry: ProjectRegistry,
) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("edit_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg, approval_timeout_s=10.0)

    # Create two pending from different clients, don't resolve.
    p_tg = await m.request_approval(
        reg.lookup("edit_file"), {}, _ctx(project_registry, client="telegram")
    )
    p_ide = await m.request_approval(
        reg.lookup("edit_file"), {}, _ctx(project_registry, client="ide")
    )
    try:
        tg_only = await m.list_pending(client="telegram")
        ide_only = await m.list_pending(client="ide")
        all_pending = await m.list_pending()
        assert {p.approval_id for p in tg_only} == {p_tg.approval_id}
        assert {p.approval_id for p in ide_only} == {p_ide.approval_id}
        assert {p.approval_id for p in all_pending} == {
            p_tg.approval_id,
            p_ide.approval_id,
        }
    finally:
        # Clean up to avoid dangling futures raising "never awaited"
        # warnings at teardown.
        await m.resolve_approval(p_tg.approval_id, "reject")
        await m.resolve_approval(p_ide.approval_id, "reject")


# --------------------------------------------------------------- args summary


def test_summarise_args_empty() -> None:
    assert _summarise_args({}) == "(no args)"


def test_summarise_args_short_fields() -> None:
    s = _summarise_args({"path": "foo.py", "n": 42})
    assert "path='foo.py'" in s
    assert "n=42" in s


def test_summarise_args_truncates_long_values() -> None:
    long_val = "x" * 500
    s = _summarise_args({"content": long_val})
    # The value portion is truncated to ~60 chars.
    assert len(s) < 210
    assert s.endswith("...")


def test_summarise_args_truncates_overall() -> None:
    # Many short fields can still overflow the overall cap.
    args = {f"k{i}": "short" for i in range(50)}
    s = _summarise_args(args)
    assert len(s) <= 200


# --------------------------------------------------------------- client per-token


async def test_per_client_override_to_auto(
    project_registry: ProjectRegistry,
) -> None:
    """IDE per-client override elevates an ASK tool to AUTO."""
    policy = ToolPolicy.from_config({"per_client": {"ide": {"write_file": "auto"}}})
    reg = ToolRegistry(policy)
    reg.register(_mk_tool("write_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("write_file"),
        {},
        _ctx(project_registry, client="ide"),
    )
    assert decision.execute is True
    assert decision.reason == "auto"


# --------------------------------------------------------------- placeholders


def test_trust_session_and_clear_session_are_noop(
    project_registry: ProjectRegistry,
) -> None:
    """Placeholders for Task 8c; exist so callers don't conditionally import."""
    reg = ToolRegistry()
    m = ApprovalMiddleware(reg)
    m.trust_session("main", "edit_file")
    m.clear_session("main")


# --------------------------------------------------------------- deny list


async def test_deny_list_blocks_before_bucket_resolution(
    project_registry: ProjectRegistry,
) -> None:
    """Even if the tool's bucket is AUTO, a deny-list hit takes
    precedence. This is the 'non-overridable floor' rule: policy
    buckets are the ceiling, deny list is the floor."""
    reg = ToolRegistry()
    reg.register(
        _mk_tool(
            "project_shell",
            ApprovalBucket.AUTO,
            shell_command_for=lambda args: args.get("command", ""),
        )
    )
    m = ApprovalMiddleware(reg)

    decision = await m.check(
        reg.lookup("project_shell"),
        {"command": "rm -rf /"},
        _ctx(project_registry),
    )
    assert not decision.execute
    assert decision.reason == "denied_deny_list"
    # Detail should mention the pattern label so operators can tell
    # what got blocked without reading the source.
    assert "root filesystem" in decision.detail.lower()


async def test_deny_list_blocks_even_on_block_bucket(
    project_registry: ProjectRegistry,
) -> None:
    """BLOCK rejects with reason=blocked; deny-list rejects with
    reason=denied_deny_list. Deny list runs first, so a destructive
    command to a BLOCK-bucket tool still reports deny-list, not
    block. Audit log consumers (Task 13) use reason to route
    severity."""
    reg = ToolRegistry()
    reg.register(
        _mk_tool(
            "project_shell",
            ApprovalBucket.BLOCK,
            shell_command_for=lambda args: args.get("command", ""),
        )
    )
    m = ApprovalMiddleware(reg)

    decision = await m.check(
        reg.lookup("project_shell"),
        {"command": "git push --force origin main"},
        _ctx(project_registry),
    )
    assert not decision.execute
    assert decision.reason == "denied_deny_list"


async def test_deny_list_ignores_tools_without_shell_hook(
    project_registry: ProjectRegistry,
) -> None:
    """The majority of tools don't touch a shell with model input;
    the deny list should be a no-op for them (bucket resolution
    runs normally)."""
    reg = ToolRegistry()
    reg.register(_mk_tool("read_file", ApprovalBucket.AUTO))
    m = ApprovalMiddleware(reg)

    decision = await m.check(
        reg.lookup("read_file"),
        {"path": "rm -rf /"},  # scary-looking ARG, but not a shell command
        _ctx(project_registry),
    )
    # AUTO, not denied — read_file isn't a shell-exposing tool,
    # its path arg is validated against traversal downstream.
    assert decision.execute
    assert decision.reason == "auto"


async def test_deny_list_allows_benign_command_via_shell_hook(
    project_registry: ProjectRegistry,
) -> None:
    """shell_command_for returns a safe string → normal bucket
    resolution kicks in."""
    reg = ToolRegistry()
    reg.register(
        _mk_tool(
            "project_shell",
            ApprovalBucket.AUTO,
            shell_command_for=lambda args: args.get("command", ""),
        )
    )
    m = ApprovalMiddleware(reg)

    decision = await m.check(
        reg.lookup("project_shell"),
        {"command": "ls -la"},
        _ctx(project_registry),
    )
    assert decision.execute
    assert decision.reason == "auto"


async def test_deny_list_empty_shell_command_skipped(
    project_registry: ProjectRegistry,
) -> None:
    """If shell_command_for returns None / empty, skip the check
    entirely (don't match the empty-string regex false positives)."""
    reg = ToolRegistry()
    reg.register(
        _mk_tool(
            "project_shell",
            ApprovalBucket.AUTO,
            shell_command_for=lambda args: None,
        )
    )
    m = ApprovalMiddleware(reg)

    decision = await m.check(
        reg.lookup("project_shell"),
        {"command": "whatever"},
        _ctx(project_registry),
    )
    assert decision.execute  # normal bucket applies


async def test_deny_list_does_not_prompt_on_ask_when_denied(
    project_registry: ProjectRegistry,
) -> None:
    """Deny-list rejection short-circuits BEFORE the approval
    prompt is created. The user shouldn't be asked whether
    they want to rm -rf /."""
    reg = ToolRegistry()
    reg.register(
        _mk_tool(
            "project_shell",
            ApprovalBucket.ASK,
            shell_command_for=lambda args: args.get("command", ""),
        )
    )
    m = ApprovalMiddleware(reg, approval_timeout_s=0.05)

    # If the deny-list didn't short-circuit, this would block for
    # 50ms waiting on the (nonexistent) user tap. With the deny
    # list working, we return synchronously with reason=denied.
    decision = await m.check(
        reg.lookup("project_shell"),
        {"command": "rm -rf /"},
        _ctx(project_registry),
    )
    assert decision.reason == "denied_deny_list"
    # And no approval is pending.
    async with m._lock:
        assert m._pending == {}

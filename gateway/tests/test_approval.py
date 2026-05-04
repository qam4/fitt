"""Tests for Phase 4 Task 8 (slim) approval middleware.

The slim version only cares about three bucket outcomes:
- ``auto`` → executes.
- ``block`` → doesn't execute, blocked reason.
- anything needing human input → rejected reason until the
  Telegram UI lands in Task 9.

Deeper tests (deny list, session trust, YOLO windows, audit
integration) come with Tasks 12/13 and when the ask UI lands.
"""

from __future__ import annotations

from typing import Any

import pytest

from gateway.approval import ApprovalMiddleware
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
    name: str, bucket: ApprovalBucket = ApprovalBucket.ASK, *, kind: str = "inline"
) -> Tool:
    return Tool(
        name=name,
        description=f"stub for {name}",
        schema={"type": "object", "properties": {}},
        callable=_noop,
        default_bucket=bucket,
        requires_project=False,
        kind=kind,  # type: ignore[arg-type]
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
    # Other clients still see the ASK default for that tool.
    decision_ide = await m.check(
        reg.lookup("write_file"),
        {},
        _ctx(project_registry, client="ide"),
    )
    assert decision_ide.execute is False
    assert decision_ide.reason == "rejected"


# --------------------------------------------------------------- ask


async def test_ask_bucket_is_rejected_until_ui_lands(
    project_registry: ProjectRegistry,
) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("edit_file", ApprovalBucket.ASK))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("edit_file"),
        {},
        _ctx(project_registry),
    )
    assert decision.execute is False
    assert decision.reason == "rejected"
    assert "approval UI" in decision.detail


async def test_trust_session_bucket_also_rejected(
    project_registry: ProjectRegistry,
) -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("git_commit", ApprovalBucket.TRUST_SESSION))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("git_commit"),
        {},
        _ctx(project_registry),
    )
    assert decision.execute is False
    assert decision.reason == "rejected"


async def test_yolo_bucket_rejected_in_slim_version(
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


# --------------------------------------------------------------- client passes through


async def test_ide_gets_auto_for_read_tools(project_registry: ProjectRegistry) -> None:
    """IDE client + AUTO-default tool + no overrides = execute."""
    reg = ToolRegistry()
    reg.register(_mk_tool("read_file", ApprovalBucket.AUTO))
    m = ApprovalMiddleware(reg)
    decision = await m.check(
        reg.lookup("read_file"),
        {},
        _ctx(project_registry, client="ide"),
    )
    assert decision.execute is True


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


# --------------------------------------------------------------- placeholder methods


def test_trust_session_and_clear_session_are_noop(
    project_registry: ProjectRegistry,
) -> None:
    """Placeholders for Task 9; exist so callers don't conditionally import."""
    reg = ToolRegistry()
    m = ApprovalMiddleware(reg)
    # Should not raise.
    m.trust_session("main", "edit_file")
    m.clear_session("main")

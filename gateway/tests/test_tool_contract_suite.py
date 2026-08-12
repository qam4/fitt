"""The contract suite against the real registry and real tools.

No model, no tunnel — so this can gate CI (Property 2). It builds the app
the way boot does, registers a fixture project, and runs every declared
check against the real tool implementations.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from gateway.tool_contract_suite import EXEMPT, build_project, default_checks, init_git_repo
from gateway.tool_contracts import run_contract_checks

_PROJECT = "sample"


@pytest.fixture()
def contract_env(tmp_path: Path, monkeypatch: Any) -> Any:
    """A real app on a temp FITT home, with a registered project."""
    monkeypatch.setenv("FITT_HOME", str(tmp_path))

    from gateway.app import create_app
    from gateway.tools import ToolContext

    from ._fixtures import build_test_config

    app = create_app(build_test_config(tmp_path, memory_enabled=True))

    from gateway.projects import Project

    root = build_project(tmp_path)
    init_git_repo(root)
    app.state.project_registry.add(
        Project(name=_PROJECT, ssh_host="", path=str(root), test_command="pytest -q")
    )

    ctx = ToolContext(
        client="cli",
        session_key="main",
        projects=app.state.project_registry,
        backend=app.state.execution_backend,
        policy=app.state.tool_registry.policy,
        audit=getattr(app.state, "audit", None),
        cron=getattr(app.state, "cron", None),
        events=getattr(app.state, "events", None),
        local_shell=getattr(app.state, "local_shell", None),
        lessons=getattr(app.state, "lessons", None),
        turns=getattr(app.state, "turns", None),
        plan_store=getattr(app.state, "plan_store", None),
        retrieval=getattr(app.state, "retrieval_provider", None),
        todos=getattr(app.state, "todos", None),
    )
    return app, ctx


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_declared_contracts_hold_against_the_real_tools(contract_env: Any) -> None:
    app, ctx = contract_env

    report = await run_contract_checks(
        app.state.tool_registry, ctx, default_checks(_PROJECT), exempt=EXEMPT
    )

    assert not report.failed, report.render()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
async def test_report_names_registered_tools_with_no_check(contract_env: Any) -> None:
    """The gap is the point: this number should shrink over the spec's
    phases, and a newly registered tool must land here automatically."""
    app, ctx = contract_env

    report = await run_contract_checks(
        app.state.tool_registry, ctx, default_checks(_PROJECT), exempt=EXEMPT
    )

    # Not asserting an exact list — that would need editing on every new
    # tool, which is the rot this design avoids. Assert the mechanism.
    assert isinstance(report.unchecked, list)
    for name in report.unchecked:
        assert name not in EXEMPT

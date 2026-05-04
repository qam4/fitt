"""Tests for Phase 4 Task 7 git tools.

Covers ``git_status`` and ``git_diff``. The execution backend is
replaced with a stub that records every ``run_shell`` call and
returns canned ShellResult values, so we can assert on the exact
argv without spawning git subprocesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gateway.projects import Project, ProjectRegistry
from gateway.tools import (
    ApprovalBucket,
    ToolContext,
    ToolRegistry,
    build_git_tools,
)
from gateway.tools.backend import ShellResult

# --------------------------------------------------------------- stubs


@dataclass
class FakeBackend:
    """Records invocations; returns queued ShellResults."""

    responses: list[ShellResult] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run_shell(
        self,
        project: Project,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_secs: int = 300,
        extra_env: dict[str, str] | None = None,
    ) -> ShellResult:
        self.calls.append(
            {
                "project": project.name,
                "ssh_host": project.ssh_host,
                "cmd": list(cmd),
                "cwd": cwd,
                "timeout_secs": timeout_secs,
            }
        )
        if not self.responses:
            return ShellResult(exit=0, stdout="", stderr="", timed_out=False)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(exit=0, stdout=stdout, stderr=stderr, timed_out=False)


def _err(exit_code: int, stderr: str) -> ShellResult:
    return ShellResult(exit=exit_code, stdout="", stderr=stderr, timed_out=False)


def _timeout(stderr: str = "timed out") -> ShellResult:
    return ShellResult(exit=-1, stdout="", stderr=stderr, timed_out=True)


# --------------------------------------------------------------- fixtures


@pytest.fixture
def project_registry(tmp_path: Path) -> ProjectRegistry:
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    reg.add(
        Project(
            name="hub",
            ssh_host="",
            path="/hub/repo",
            test_command="pytest -q",
        )
    )
    reg.add(
        Project(
            name="remote",
            ssh_host="laptop.tailnet",
            path="/home/fred/code/x",
            test_command="pytest -q",
        )
    )
    return reg


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in build_git_tools():
        reg.register(t)
    return reg


def _ctx(projects: ProjectRegistry, backend: FakeBackend) -> ToolContext:
    return ToolContext(
        client="ide",
        session_key="main",
        projects=projects,
        backend=backend,
    )


# --------------------------------------------------------------- registration


def test_builder_registers_two_tools(tool_registry: ToolRegistry) -> None:
    assert tool_registry.list_names() == ["git_diff", "git_status"]


def test_all_git_tools_default_to_auto(tool_registry: ToolRegistry) -> None:
    for t in tool_registry.list_all():
        assert t.default_bucket == ApprovalBucket.AUTO, t.name
        assert t.requires_project is True


# --------------------------------------------------------------- git_status


async def test_git_status_porcelain_v1_and_branch(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(
        responses=[_ok(stdout="## main...origin/main\n M src/a.py\n?? notes.txt\n")]
    )
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert "## main" in result.payload
    assert " M src/a.py" in result.payload
    assert backend.calls[0]["cmd"] == ["git", "status", "--porcelain=v1", "-b"]
    assert backend.calls[0]["timeout_secs"] == 30


async def test_git_status_works_on_ssh_project(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="## main\n")])
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({"project": "remote"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert backend.calls[0]["ssh_host"] == "laptop.tailnet"


async def test_git_status_missing_project(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({}, _ctx(project_registry, backend))
    assert result.is_error
    assert "project" in result.payload
    assert backend.calls == []


async def test_git_status_nonzero_surfaces_stderr(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_err(128, "fatal: not a git repository\n")])
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "not a git repository" in result.payload


async def test_git_status_timeout(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_timeout("command timed out after 30s")])
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "timed out" in result.payload


async def test_git_status_truncates_long_output(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    long = "## main\n" + ("?? file.txt\n" * 6000)
    backend = FakeBackend(responses=[_ok(stdout=long)])
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert "more bytes truncated" in result.payload
    assert len(result.payload) < len(long)


async def test_git_status_missing_backend(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    ctx = ToolContext(
        client="ide",
        session_key="main",
        projects=project_registry,
        backend=None,
    )
    tool = tool_registry.lookup("git_status")
    result = await tool.callable({"project": "hub"}, ctx)
    assert result.is_error
    assert "no execution backend" in result.payload


# --------------------------------------------------------------- git_diff


async def test_git_diff_defaults_to_working_tree(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="diff --git a/x b/x\n")])
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert backend.calls[0]["cmd"] == ["git", "--no-pager", "diff"]
    assert backend.calls[0]["timeout_secs"] == 60


async def test_git_diff_with_ref(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="diff body\n")])
    tool = tool_registry.lookup("git_diff")
    await tool.callable(
        {"project": "hub", "ref": "HEAD~1"},
        _ctx(project_registry, backend),
    )
    assert backend.calls[0]["cmd"] == ["git", "--no-pager", "diff", "HEAD~1"]


async def test_git_diff_with_ref_and_path_uses_double_dash(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="diff body\n")])
    tool = tool_registry.lookup("git_diff")
    await tool.callable(
        {"project": "hub", "ref": "main", "path": "src/app.py"},
        _ctx(project_registry, backend),
    )
    assert backend.calls[0]["cmd"] == [
        "git",
        "--no-pager",
        "diff",
        "main",
        "--",
        "src/app.py",
    ]


async def test_git_diff_path_without_ref_still_separated(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """`--` gets inserted whenever a path is supplied, so a path
    starting with `-` can't be misread as an option."""
    backend = FakeBackend(responses=[_ok(stdout="")])
    tool = tool_registry.lookup("git_diff")
    await tool.callable(
        {"project": "hub", "path": "README.md"},
        _ctx(project_registry, backend),
    )
    assert backend.calls[0]["cmd"] == [
        "git",
        "--no-pager",
        "diff",
        "--",
        "README.md",
    ]


async def test_git_diff_no_changes_is_ok(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="")])
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert result.payload == "(no changes)"


async def test_git_diff_surfaces_error(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_err(128, "fatal: bad revision 'nopenope'\n")])
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable(
        {"project": "hub", "ref": "nopenope"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "bad revision" in result.payload


async def test_git_diff_truncates_huge_diff(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    huge = "diff --git a/x b/x\n" + ("+ line\n" * 20_000)
    backend = FakeBackend(responses=[_ok(stdout=huge)])
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert "more bytes truncated" in result.payload


# ---- ref / path validation --------------------------------------


@pytest.mark.parametrize(
    "bad_ref",
    [
        "-main",
        "--all",
        "foo;rm -rf /",
        "foo bar",
        "foo|bar",
        "foo$(evil)",
        "foo`evil`",
    ],
)
async def test_git_diff_rejects_dangerous_ref(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    bad_ref: str,
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable(
        {"project": "hub", "ref": bad_ref},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "ref rejected" in result.payload
    assert backend.calls == []  # never reached backend


@pytest.mark.parametrize(
    "bad_path",
    [
        "-flag-like",
        "../escape",
        "nested/../escape",
    ],
)
async def test_git_diff_rejects_dangerous_path(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    bad_path: str,
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable(
        {"project": "hub", "path": bad_path},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "path rejected" in result.payload
    assert backend.calls == []


async def test_git_diff_accepts_common_ref_forms(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """The ref validator shouldn't reject ordinary usage."""
    backend = FakeBackend(responses=[_ok(stdout="")])
    tool = tool_registry.lookup("git_diff")
    for ref in ["main", "HEAD~1", "abc123..HEAD", "origin/main", "v0.1.0"]:
        backend.calls.clear()
        backend.responses = [_ok(stdout="")]
        result = await tool.callable(
            {"project": "hub", "ref": ref},
            _ctx(project_registry, backend),
        )
        assert not result.is_error, (ref, result.payload)
        assert ref in backend.calls[0]["cmd"]


async def test_git_diff_ref_wrong_type_errors(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable({"project": "hub", "ref": 42}, _ctx(project_registry, backend))
    assert result.is_error
    assert "ref" in result.payload


async def test_git_diff_path_wrong_type_errors(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("git_diff")
    result = await tool.callable({"project": "hub", "path": ["x"]}, _ctx(project_registry, backend))
    assert result.is_error
    assert "path" in result.payload

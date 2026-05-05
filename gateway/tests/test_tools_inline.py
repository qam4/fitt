"""Tests for Phase 4 inline tools.

Covers ``list_capabilities`` and the four ``spec_*`` tools. The
ExecutionBackend is stubbed so tests don't shell out: we assert
on the exact argv the backend receives and control the
ShellResult it returns.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from gateway.projects import Project, ProjectRegistry
from gateway.tools import (
    ApprovalBucket,
    ToolContext,
    ToolRegistry,
    build_inline_tools,
)
from gateway.tools.backend import ShellResult

# --------------------------------------------------------------- stubs


@dataclass
class FakeBackend:
    """Records invocations; returns queued ShellResults.

    Each call reads ``responses[call_index]`` and falls back to
    the last entry once exhausted. The ``calls`` list is what
    tests assert on to verify argv shape.
    """

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


# --------------------------------------------------------------- fixtures


@pytest.fixture
def hub_project(tmp_path: Path) -> tuple[ProjectRegistry, Project]:
    """A registered hub-local project. The backend still gets
    invoked — the "hub-local" distinction only affects how the
    real ExecutionBackend dispatches, which we mock out."""
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    project = Project(
        name="hub",
        ssh_host="",
        path="/hub/repo",
        test_command="pytest -q",
    )
    reg.add(project)
    return reg, project


@pytest.fixture
def ssh_project(tmp_path: Path) -> tuple[ProjectRegistry, Project]:
    """An ssh-backed project."""
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    project = Project(
        name="laptop",
        ssh_host="user@laptop.tailnet",
        path="/c/src/home-ai-cluster",
        test_command="pytest -q",
    )
    reg.add(project)
    return reg, project


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in build_inline_tools(reg):
        reg.register(t)
    return reg


def _ctx(
    projects: ProjectRegistry,
    backend: FakeBackend | None = None,
) -> ToolContext:
    return ToolContext(
        client="ide",
        session_key="main",
        projects=projects,
        backend=backend,
    )


# --------------------------------------------------------------- registration


def test_builder_returns_all_five_tools(tool_registry: ToolRegistry) -> None:
    names = tool_registry.list_names()
    assert names == [
        "list_capabilities",
        "spec_list",
        "spec_mark_task",
        "spec_next_task",
        "spec_read",
    ]


def test_all_inline_tools_default_to_auto(tool_registry: ToolRegistry) -> None:
    for t in tool_registry.list_all():
        assert t.default_bucket == ApprovalBucket.AUTO, t.name


# --------------------------------------------------------------- list_capabilities


async def test_list_capabilities_returns_registry_json(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("list_capabilities")
    result = await tool.callable({}, _ctx(projects))
    assert not result.is_error
    parsed = json.loads(result.payload)
    names = [e["name"] for e in parsed]
    assert "list_capabilities" in names
    assert "spec_list" in names
    for entry in parsed:
        assert entry.keys() == {
            "name",
            "description",
            "bucket",
            "kind",
            "requires_project",
        }


# --------------------------------------------------------------- spec_list


async def test_spec_list_empty_when_dir_missing(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    # `ls` returns nothing when the `test -d` guard short-circuits.
    backend = FakeBackend(responses=[_ok(stdout="")])
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "hub"}, _ctx(projects, backend))
    assert not result.is_error
    assert json.loads(result.payload) == []
    # Argv check: we dispatch through a guarded sh -c ...
    assert backend.calls[0]["cmd"][0] == "sh"
    assert backend.calls[0]["cmd"][1] == "-c"
    assert "/hub/repo/.kiro/specs" in backend.calls[0]["cmd"][2]


async def test_spec_list_sorts_features(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    backend = FakeBackend(responses=[_ok(stdout="zebra\nalpha\nmango\n")])
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "hub"}, _ctx(projects, backend))
    assert not result.is_error
    assert json.loads(result.payload) == ["alpha", "mango", "zebra"]


async def test_spec_list_filters_dotfiles(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    backend = FakeBackend(responses=[_ok(stdout="phase4\n.stash\nalpha\n")])
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "hub"}, _ctx(projects, backend))
    assert not result.is_error
    assert json.loads(result.payload) == ["alpha", "phase4"]


async def test_spec_list_works_against_ssh_project(
    tool_registry: ToolRegistry,
    ssh_project: tuple[ProjectRegistry, Project],
) -> None:
    """ssh_host is forwarded to the backend; the tool doesn't care."""
    projects, _ = ssh_project
    backend = FakeBackend(responses=[_ok(stdout="phase4\n")])
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "laptop"}, _ctx(projects, backend))
    assert not result.is_error
    assert json.loads(result.payload) == ["phase4"]
    assert backend.calls[0]["ssh_host"] == "user@laptop.tailnet"


async def test_spec_list_unknown_project_errors(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "ghost"}, _ctx(projects, FakeBackend()))
    assert result.is_error
    assert "Unknown project" in result.payload


async def test_spec_list_missing_backend_errors(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "hub"}, _ctx(projects))
    assert result.is_error
    assert "no execution backend" in result.payload


# --------------------------------------------------------------- spec_read


async def test_spec_read_returns_concatenated_markdown(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    # Three cats, one per spec file.
    backend = FakeBackend(
        responses=[
            _ok(stdout="# R\n\nrequirement body\n"),
            _ok(stdout="# D\n\ndesign body\n"),
            _ok(stdout="# T\n\n- [ ] 1a. first\n"),
        ]
    )
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": "phase4"}, _ctx(projects, backend))
    assert not result.is_error
    assert "## requirements.md" in result.payload
    assert "requirement body" in result.payload
    assert "## design.md" in result.payload
    assert "design body" in result.payload
    assert "## tasks.md" in result.payload
    assert "1a. first" in result.payload
    # Three backend invocations, one per file.
    assert len(backend.calls) == 3


async def test_spec_read_missing_file_labelled(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    # requirements.md present; design.md and tasks.md missing.
    backend = FakeBackend(
        responses=[
            _ok(stdout="r only"),
            _ok(stdout="__FITT_MISSING__\n"),
            _ok(stdout="__FITT_MISSING__\n"),
        ]
    )
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": "half"}, _ctx(projects, backend))
    assert not result.is_error
    assert "r only" in result.payload
    assert "(missing)" in result.payload


async def test_spec_read_all_missing_is_an_error(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    """All three files missing = the feature doesn't exist; surface
    a clean "not found" error rather than three missing-stubs."""
    projects, _ = hub_project
    backend = FakeBackend(
        responses=[
            _ok(stdout="__FITT_MISSING__\n"),
            _ok(stdout="__FITT_MISSING__\n"),
            _ok(stdout="__FITT_MISSING__\n"),
        ]
    )
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": "ghost"}, _ctx(projects, backend))
    assert result.is_error
    assert "Spec feature not found" in result.payload


@pytest.mark.parametrize(
    "bad_feature",
    ["../escape", "..", "a/b", "sub\\dir", ".hidden"],
)
async def test_spec_read_rejects_path_traversal(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
    bad_feature: str,
) -> None:
    projects, _ = hub_project
    backend = FakeBackend()
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable(
        {"project": "hub", "feature": bad_feature},
        _ctx(projects, backend),
    )
    assert result.is_error
    assert "Invalid feature name" in result.payload
    assert backend.calls == []  # never reached the backend


# --------------------------------------------------------------- spec_next_task


async def test_spec_next_task_returns_first_unchecked(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tasks = dedent(
        """
        # Tasks

        - [x] 1a. done
        - [x] 1b. also done
        - [ ] 2a. first open
        - [ ] 2b. later open
        """
    ).strip()
    backend = FakeBackend(responses=[_ok(stdout=tasks)])
    tool = tool_registry.lookup("spec_next_task")
    result = await tool.callable(
        {"project": "hub", "feature": "phase4"},
        _ctx(projects, backend),
    )
    assert not result.is_error
    parsed = json.loads(result.payload)
    assert parsed == {"task_id": "2a", "text": "first open"}


async def test_spec_next_task_all_done(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    backend = FakeBackend(responses=[_ok(stdout="- [x] 1a. done\n- [x] 1b. done\n")])
    tool = tool_registry.lookup("spec_next_task")
    result = await tool.callable({"project": "hub", "feature": "f"}, _ctx(projects, backend))
    assert not result.is_error
    assert json.loads(result.payload) == {"task_id": None, "text": None}


async def test_spec_next_task_missing_tasks_file(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    backend = FakeBackend(responses=[_ok(stdout="__FITT_MISSING__\n")])
    tool = tool_registry.lookup("spec_next_task")
    result = await tool.callable(
        {"project": "hub", "feature": "notasks"},
        _ctx(projects, backend),
    )
    assert result.is_error
    assert "No tasks.md" in result.payload


# --------------------------------------------------------------- spec_mark_task


async def test_spec_mark_task_flips_checkbox(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tasks = dedent(
        """
        # Tasks

        - [ ] 1a. first
        - [ ] 1b. second
        - [ ] 1c. third
        """
    ).strip()
    # Two calls: (1) cat to read, (2) sh -c 'base64 -d > tmp && mv tmp path'
    backend = FakeBackend(responses=[_ok(stdout=tasks), _ok(stdout="")])
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "1b"},
        _ctx(projects, backend),
    )
    assert not result.is_error
    assert "Marked task '1b' done" in result.payload

    # Verify the write call actually embeds the new content
    # (base64-decoded) — we shouldn't have flipped 1a or 1c.
    write_cmd = backend.calls[1]["cmd"][2]
    # The payload is embedded as a base64 argument after `printf %s `.
    # Extract it.
    import re

    m = re.search(r"printf %s (?:'|\")?([A-Za-z0-9+/=]+)", write_cmd)
    assert m, write_cmd
    decoded = base64.b64decode(m.group(1)).decode("utf-8")
    assert "- [ ] 1a. first" in decoded
    assert "- [x] 1b. second" in decoded
    assert "- [ ] 1c. third" in decoded


async def test_spec_mark_task_already_done_is_noop(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    backend = FakeBackend(responses=[_ok(stdout="- [x] 1a. done\n")])
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "1a"},
        _ctx(projects, backend),
    )
    assert not result.is_error
    assert "already marked done" in result.payload
    # Only the read dispatched; no write.
    assert len(backend.calls) == 1


async def test_spec_mark_task_unknown_id_errors(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    backend = FakeBackend(responses=[_ok(stdout="- [ ] 1a. first\n")])
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "9z"},
        _ctx(projects, backend),
    )
    assert result.is_error
    assert "No task '9z'" in result.payload


async def test_spec_mark_task_ambiguous_id_errors(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tasks = dedent(
        """
        - [ ] 1a. first
        - [ ] 1a. also first (duplicate id)
        """
    ).strip()
    backend = FakeBackend(responses=[_ok(stdout=tasks)])
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "1a"},
        _ctx(projects, backend),
    )
    assert result.is_error
    assert "ambiguous" in result.payload


async def test_spec_mark_task_missing_args_errors(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable({"project": "hub"}, _ctx(projects, FakeBackend()))
    assert result.is_error
    assert "feature" in result.payload

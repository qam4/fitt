"""Tests for Phase 4 Task 4 inline tools.

Covers ``list_capabilities``, ``spec_list``, ``spec_read``,
``spec_next_task``, ``spec_mark_task``. All tools run hub-local;
the SSH backend lands in Task 5 and only then do we exercise the
ssh_host code path.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from gateway.projects import Project, ProjectRegistry
from gateway.tools import (
    ApprovalBucket,
    ToolContext,
    ToolRegistry,
    build_inline_tools,
)

# --------------------------------------------------------------- fixtures


@pytest.fixture
def hub_project(tmp_path: Path) -> tuple[ProjectRegistry, Project]:
    """A registered project rooted at ``tmp_path/repo``, hub-local."""
    repo = tmp_path / "repo"
    repo.mkdir()
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    project = Project(
        name="hub",
        ssh_host="",
        path=str(repo),
        test_command="pytest -q",
        build_command="",
    )
    reg.add(project)
    return reg, project


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """A ToolRegistry pre-populated with all Task 4 inline tools."""
    reg = ToolRegistry()
    for t in build_inline_tools(reg):
        reg.register(t)
    return reg


def _ctx(projects: ProjectRegistry) -> ToolContext:
    return ToolContext(client="ide", session_key="main", projects=projects)


def _write_spec(
    project: Project,
    feature: str,
    *,
    requirements: str = "# Requirements\n",
    design: str = "# Design\n",
    tasks: str = "# Tasks\n",
) -> Path:
    d = Path(project.path) / ".kiro" / "specs" / feature
    d.mkdir(parents=True, exist_ok=True)
    (d / "requirements.md").write_text(requirements, encoding="utf-8")
    (d / "design.md").write_text(design, encoding="utf-8")
    (d / "tasks.md").write_text(tasks, encoding="utf-8")
    return d


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


@pytest.mark.asyncio
async def test_list_capabilities_returns_registry_json(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("list_capabilities")
    result = await tool.callable({}, _ctx(projects))
    assert not result.is_error
    parsed = json.loads(result.payload)
    names = [e["name"] for e in parsed]
    assert "list_capabilities" in names
    assert "spec_list" in names
    # Each entry carries description, bucket, kind, requires_project.
    for entry in parsed:
        assert entry.keys() == {
            "name",
            "description",
            "bucket",
            "kind",
            "requires_project",
        }


# --------------------------------------------------------------- spec_list


@pytest.mark.asyncio
async def test_spec_list_empty_when_no_specs_dir(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "hub"}, _ctx(projects))
    assert not result.is_error
    assert json.loads(result.payload) == []


@pytest.mark.asyncio
async def test_spec_list_returns_sorted_feature_names(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    _write_spec(project, "zebra")
    _write_spec(project, "alpha")
    _write_spec(project, "mango")
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "hub"}, _ctx(projects))
    assert not result.is_error
    assert json.loads(result.payload) == ["alpha", "mango", "zebra"]


@pytest.mark.asyncio
async def test_spec_list_unknown_project_errors(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "ghost"}, _ctx(projects))
    assert result.is_error
    assert "Unknown project" in result.payload


@pytest.mark.asyncio
async def test_ssh_project_refused_until_backend_ready(
    tmp_path: Path, tool_registry: ToolRegistry
) -> None:
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    reg.add(
        Project(
            name="laptop-project",
            ssh_host="laptop.tailnet",
            path="/home/fred/code/x",
        )
    )
    tool = tool_registry.lookup("spec_list")
    result = await tool.callable({"project": "laptop-project"}, _ctx(reg))
    assert result.is_error
    assert "SSH backend not yet available" in result.payload


# --------------------------------------------------------------- spec_read


@pytest.mark.asyncio
async def test_spec_read_returns_concatenated_markdown(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    _write_spec(
        project,
        "phase4",
        requirements="# R\n\nrequirement body\n",
        design="# D\n\ndesign body\n",
        tasks="# T\n\n- [ ] 1a. first\n",
    )
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": "phase4"}, _ctx(projects))
    assert not result.is_error
    assert "## requirements.md" in result.payload
    assert "requirement body" in result.payload
    assert "## design.md" in result.payload
    assert "design body" in result.payload
    assert "## tasks.md" in result.payload
    assert "1a. first" in result.payload


@pytest.mark.asyncio
async def test_spec_read_missing_file_labelled(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    # Only create requirements.md
    d = Path(project.path) / ".kiro" / "specs" / "half"
    d.mkdir(parents=True)
    (d / "requirements.md").write_text("r only", encoding="utf-8")

    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": "half"}, _ctx(projects))
    assert not result.is_error
    assert "## requirements.md" in result.payload
    assert "r only" in result.payload
    assert "## design.md" in result.payload
    assert "(missing)" in result.payload


@pytest.mark.asyncio
async def test_spec_read_unknown_feature_errors(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": "nope"}, _ctx(projects))
    assert result.is_error
    assert "Spec feature not found" in result.payload


@pytest.mark.parametrize(
    "bad_feature",
    ["../escape", "..", "a/b", "sub\\dir", ".hidden"],
)
@pytest.mark.asyncio
async def test_spec_read_rejects_path_traversal(
    tool_registry: ToolRegistry,
    hub_project: tuple[ProjectRegistry, Project],
    bad_feature: str,
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_read")
    result = await tool.callable({"project": "hub", "feature": bad_feature}, _ctx(projects))
    assert result.is_error
    assert "Invalid feature name" in result.payload


# --------------------------------------------------------------- spec_next_task


@pytest.mark.asyncio
async def test_spec_next_task_returns_first_unchecked(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    tasks = dedent(
        """
        # Tasks

        - [x] 1a. done
        - [x] 1b. also done
        - [ ] 2a. first open
        - [ ] 2b. later open
        """
    ).strip()
    _write_spec(project, "phase4", tasks=tasks)

    tool = tool_registry.lookup("spec_next_task")
    result = await tool.callable({"project": "hub", "feature": "phase4"}, _ctx(projects))
    assert not result.is_error
    parsed = json.loads(result.payload)
    assert parsed == {"task_id": "2a", "text": "first open"}


@pytest.mark.asyncio
async def test_spec_next_task_all_done(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    _write_spec(project, "f", tasks="- [x] 1a. done\n- [x] 1b. done\n")
    tool = tool_registry.lookup("spec_next_task")
    result = await tool.callable({"project": "hub", "feature": "f"}, _ctx(projects))
    assert not result.is_error
    parsed = json.loads(result.payload)
    assert parsed == {"task_id": None, "text": None}


@pytest.mark.asyncio
async def test_spec_next_task_missing_tasks_file(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    d = Path(project.path) / ".kiro" / "specs" / "notasks"
    d.mkdir(parents=True)
    tool = tool_registry.lookup("spec_next_task")
    result = await tool.callable({"project": "hub", "feature": "notasks"}, _ctx(projects))
    assert result.is_error
    assert "No tasks.md" in result.payload


# --------------------------------------------------------------- spec_mark_task


@pytest.mark.asyncio
async def test_spec_mark_task_flips_checkbox(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    tasks = dedent(
        """
        # Tasks

        - [ ] 1a. first
        - [ ] 1b. second
        - [ ] 1c. third
        """
    ).strip()
    _write_spec(project, "f", tasks=tasks)

    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "1b"}, _ctx(projects)
    )
    assert not result.is_error
    assert "Marked task '1b' done" in result.payload

    after = (Path(project.path) / ".kiro/specs/f/tasks.md").read_text(encoding="utf-8")
    assert "- [ ] 1a. first" in after
    assert "- [x] 1b. second" in after
    assert "- [ ] 1c. third" in after


@pytest.mark.asyncio
async def test_spec_mark_task_already_done_is_noop(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    _write_spec(project, "f", tasks="- [x] 1a. done\n")
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "1a"}, _ctx(projects)
    )
    assert not result.is_error
    assert "already marked done" in result.payload


@pytest.mark.asyncio
async def test_spec_mark_task_unknown_id_errors(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    _write_spec(project, "f", tasks="- [ ] 1a. first\n")
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "9z"}, _ctx(projects)
    )
    assert result.is_error
    assert "No task '9z'" in result.payload


@pytest.mark.asyncio
async def test_spec_mark_task_ambiguous_id_errors(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, project = hub_project
    tasks = dedent(
        """
        - [ ] 1a. first
        - [ ] 1a. also first (duplicate id)
        """
    ).strip()
    _write_spec(project, "f", tasks=tasks)
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable(
        {"project": "hub", "feature": "f", "task_id": "1a"}, _ctx(projects)
    )
    assert result.is_error
    assert "ambiguous" in result.payload


@pytest.mark.asyncio
async def test_spec_mark_task_missing_args_errors(
    tool_registry: ToolRegistry, hub_project: tuple[ProjectRegistry, Project]
) -> None:
    projects, _ = hub_project
    tool = tool_registry.lookup("spec_mark_task")
    result = await tool.callable({"project": "hub"}, _ctx(projects))
    assert result.is_error
    assert "feature" in result.payload

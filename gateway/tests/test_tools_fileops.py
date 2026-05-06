"""Tests for Phase 4 Task 6 file-access tools.

Covers ``read_file``, ``list_directory``, ``grep_repo``,
``glob_search``. The execution backend is replaced with a stub
that records every ``run_shell`` call and returns canned
:class:`~gateway.tools.backend.ShellResult` values, so we can
assert on the exact argv passed to it without spawning real
processes.
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
    build_fileops_tools,
)
from gateway.tools.backend import ShellResult

# --------------------------------------------------------------- stubs


@dataclass
class FakeBackend:
    """Records invocations; returns queued ShellResults.

    The first ``run_shell`` call returns ``responses[0]``, second
    returns ``responses[1]``, etc. If the list is exhausted the
    last entry is reused (common case: a single canned response).
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
        stdin: bytes | None = None,
    ) -> ShellResult:
        self.calls.append(
            {
                "project": project.name,
                "ssh_host": project.ssh_host,
                "cmd": list(cmd),
                "cwd": cwd,
                "timeout_secs": timeout_secs,
                "extra_env": dict(extra_env) if extra_env else None,
                "stdin": stdin,
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
    for t in build_fileops_tools():
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


def test_builder_registers_six_tools(tool_registry: ToolRegistry) -> None:
    names = tool_registry.list_names()
    assert names == [
        "edit_file",
        "glob_search",
        "grep_repo",
        "list_directory",
        "read_file",
        "write_file",
    ]


def test_read_tools_default_to_auto(tool_registry: ToolRegistry) -> None:
    """Read tools auto-approve (fast, harmless)."""
    read_tool_names = {"read_file", "list_directory", "grep_repo", "glob_search"}
    for t in tool_registry.list_all():
        if t.name in read_tool_names:
            assert t.default_bucket == ApprovalBucket.AUTO, t.name
        assert t.requires_project is True


def test_write_tools_default_to_ask(tool_registry: ToolRegistry) -> None:
    """Write tools require approval by default. Per-client policy
    (e.g. ide=auto) is configured in config.yaml, not here."""
    for name in ("write_file", "edit_file"):
        tool = tool_registry.lookup(name)
        assert tool.default_bucket == ApprovalBucket.ASK


# --------------------------------------------------------------- read_file


async def test_read_file_runs_cat_with_safe_path(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="hello world\n")])
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "hub", "path": "README.md"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert result.payload == "hello world\n"

    assert len(backend.calls) == 1
    assert backend.calls[0]["cmd"] == ["cat", "--", "README.md"]
    assert backend.calls[0]["ssh_host"] == ""
    assert backend.calls[0]["timeout_secs"] == 30


async def test_read_file_works_on_ssh_project(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="ssh content")])
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "remote", "path": "src/main.py"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert result.payload == "ssh content"
    assert backend.calls[0]["ssh_host"] == "laptop.tailnet"
    assert backend.calls[0]["cmd"] == ["cat", "--", "src/main.py"]


async def test_read_file_missing_path_arg(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("read_file")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "path" in result.payload
    assert backend.calls == []  # never reached the backend


async def test_read_file_missing_project_arg(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("read_file")
    result = await tool.callable({"path": "README.md"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "project" in result.payload


async def test_read_file_unknown_project(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("read_file")
    result = await tool.callable({"project": "ghost", "path": "x"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "Unknown project" in result.payload
    assert backend.calls == []


async def test_read_file_surfaces_nonzero_exit(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_err(1, "cat: nope.md: No such file or directory\n")])
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "hub", "path": "nope.md"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "No such file" in result.payload


async def test_read_file_surfaces_timeout(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_timeout("command timed out after 30s")])
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "hub", "path": "big.bin"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "timed out" in result.payload


async def test_read_file_truncates_large_output(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    huge = "x" * 300_000
    backend = FakeBackend(responses=[_ok(stdout=huge)])
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "hub", "path": "huge.txt"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert len(result.payload) < 300_000
    assert "more bytes truncated" in result.payload


async def test_read_file_missing_backend_on_ctx(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Sanity check on the context-wiring error path."""
    ctx = ToolContext(
        client="ide",
        session_key="main",
        projects=project_registry,
        backend=None,
    )
    tool = tool_registry.lookup("read_file")
    result = await tool.callable({"project": "hub", "path": "x"}, ctx)
    assert result.is_error
    assert "no execution backend" in result.payload


# --------------------------------------------------------------- path safety


@pytest.mark.parametrize(
    "bad_path",
    [
        "../etc/passwd",
        "..",
        "subdir/../..",
        "a/../b/../../c",
        "/etc/passwd",  # absolute, outside project
        "/other/project/file",  # absolute, outside project
    ],
)
async def test_read_file_rejects_traversal(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    bad_path: str,
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "hub", "path": bad_path},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "Path rejected" in result.payload
    assert backend.calls == []  # never reached backend


async def test_read_file_accepts_absolute_path_inside_project(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Absolute paths under the project root are OK."""
    backend = FakeBackend(responses=[_ok("content")])
    tool = tool_registry.lookup("read_file")
    result = await tool.callable(
        {"project": "hub", "path": "/hub/repo/config/foo.yaml"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert backend.calls[0]["cmd"] == ["cat", "--", "/hub/repo/config/foo.yaml"]


# --------------------------------------------------------------- list_directory


async def test_list_directory_defaults_to_project_root(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="total 0\n")])
    tool = tool_registry.lookup("list_directory")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert backend.calls[0]["cmd"] == ["ls", "-la", "--", "."]


async def test_list_directory_honours_path(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="file1\nfile2\n")])
    tool = tool_registry.lookup("list_directory")
    await tool.callable(
        {"project": "hub", "path": "subdir"},
        _ctx(project_registry, backend),
    )
    assert backend.calls[0]["cmd"] == ["ls", "-la", "--", "subdir"]


async def test_list_directory_rejects_traversal(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("list_directory")
    result = await tool.callable(
        {"project": "hub", "path": "../.."},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "Path rejected" in result.payload
    assert backend.calls == []


async def test_list_directory_surfaces_errors(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_err(2, "ls: cannot access 'nope': No such file\n")])
    tool = tool_registry.lookup("list_directory")
    result = await tool.callable(
        {"project": "hub", "path": "nope"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "No such file" in result.payload


# --------------------------------------------------------------- grep_repo


async def test_grep_repo_builds_expected_argv(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="./a.py:1:match\n")])
    tool = tool_registry.lookup("grep_repo")
    result = await tool.callable(
        {"project": "hub", "pattern": "foo.*bar"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert result.payload == "./a.py:1:match\n"
    # -rnIE for recursive / line numbers / skip binary / extended regex.
    assert backend.calls[0]["cmd"] == [
        "grep",
        "-rnIE",
        "--",
        "foo.*bar",
        ".",
    ]


async def test_grep_repo_with_path_filter(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="./src/x.py:3:hit\n")])
    tool = tool_registry.lookup("grep_repo")
    await tool.callable(
        {"project": "hub", "pattern": "TODO", "path_filter": "*.py"},
        _ctx(project_registry, backend),
    )
    assert backend.calls[0]["cmd"] == [
        "grep",
        "-rnIE",
        "--include",
        "*.py",
        "--",
        "TODO",
        ".",
    ]


async def test_grep_repo_no_match_is_ok(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """grep exit=1 means 'no matches'; should be a clean OK result."""
    backend = FakeBackend(responses=[_err(1, "")])
    tool = tool_registry.lookup("grep_repo")
    result = await tool.callable(
        {"project": "hub", "pattern": "never-there"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert result.payload == "(no matches)"


async def test_grep_repo_exit2_surfaces_as_error(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """grep exit >=2 means real trouble (bad regex, I/O error)."""
    backend = FakeBackend(responses=[_err(2, "grep: Invalid regular expression\n")])
    tool = tool_registry.lookup("grep_repo")
    result = await tool.callable(
        {"project": "hub", "pattern": "["},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "Invalid regular expression" in result.payload


async def test_grep_repo_missing_pattern(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("grep_repo")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "pattern" in result.payload


async def test_grep_repo_rejects_nonstring_path_filter(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("grep_repo")
    result = await tool.callable(
        {"project": "hub", "pattern": "x", "path_filter": 123},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "path_filter" in result.payload


# --------------------------------------------------------------- glob_search


async def test_glob_search_builds_expected_argv(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="./README.md\n./docs/quickstart.md\n")])
    tool = tool_registry.lookup("glob_search")
    result = await tool.callable(
        {"project": "hub", "pattern": "*.md"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert "README.md" in result.payload
    assert backend.calls[0]["cmd"] == [
        "find",
        ".",
        "-type",
        "f",
        "-name",
        "*.md",
    ]


async def test_glob_search_no_matches_is_ok(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="")])
    tool = tool_registry.lookup("glob_search")
    result = await tool.callable(
        {"project": "hub", "pattern": "*.nope"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert result.payload == "(no matches)"


async def test_glob_search_surfaces_errors(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_err(1, "find: invalid predicate\n")])
    tool = tool_registry.lookup("glob_search")
    result = await tool.callable(
        {"project": "hub", "pattern": "*.md"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "invalid predicate" in result.payload


async def test_glob_search_missing_pattern(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("glob_search")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "pattern" in result.payload


# --------------------------------------------------------------- write_file


async def test_write_file_pipes_content_through_stdin(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """write_file uses `cat > <path>` piped with stdin so content
    with quotes, newlines, or shell metacharacters passes through
    unchanged."""
    backend = FakeBackend(responses=[_ok()])
    tool = tool_registry.lookup("write_file")
    content = 'line 1\nline "2"\n$not_a_var\n'
    result = await tool.callable(
        {"project": "hub", "path": "src/new_file.py", "content": content},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert "wrote" in result.payload
    # The command runs sh -c so parent dirs are made and cat reads stdin.
    assert backend.calls[0]["cmd"][0] == "sh"
    assert backend.calls[0]["cmd"][1] == "-c"
    script = backend.calls[0]["cmd"][2]
    assert "mkdir -p" in script
    assert "cat > " in script
    # stdin should carry the exact content bytes.
    assert backend.calls[0]["stdin"] == content.encode("utf-8")


async def test_write_file_rejects_traversal(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("write_file")
    result = await tool.callable(
        {"project": "hub", "path": "../escape.txt", "content": "x"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "Path rejected" in result.payload
    assert backend.calls == []


async def test_write_file_rejects_project_root(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Writing to `.` / the project root itself doesn't make sense
    (there's no file to create) and would be confusing; reject."""
    backend = FakeBackend()
    tool = tool_registry.lookup("write_file")
    result = await tool.callable(
        {"project": "hub", "path": ".", "content": "x"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "project root" in result.payload


async def test_write_file_rejects_nonstring_content(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("write_file")
    result = await tool.callable(
        {"project": "hub", "path": "x.py", "content": 42},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "content" in result.payload


async def test_write_file_rejects_oversized_content(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Guard against the model trying to write a multi-megabyte
    single file (usually a bug: a dump, a long log)."""
    backend = FakeBackend()
    tool = tool_registry.lookup("write_file")
    huge = "x" * 600_000
    result = await tool.callable(
        {"project": "hub", "path": "big.bin", "content": huge},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "exceeds" in result.payload
    assert backend.calls == []


async def test_write_file_surfaces_backend_error(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_err(1, "sh: cannot create file: Permission denied\n")])
    tool = tool_registry.lookup("write_file")
    result = await tool.callable(
        {"project": "hub", "path": "src/x.py", "content": "pass"},
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "Permission denied" in result.payload


async def test_write_file_works_over_ssh(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Same tool, remote project; stdin flows through ssh."""
    backend = FakeBackend(responses=[_ok()])
    tool = tool_registry.lookup("write_file")
    result = await tool.callable(
        {"project": "remote", "path": "src/a.py", "content": "y = 2\n"},
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert backend.calls[0]["ssh_host"] == "laptop.tailnet"
    assert backend.calls[0]["stdin"] == b"y = 2\n"


# --------------------------------------------------------------- edit_file


async def test_edit_file_happy_path(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Read current file, find exactly one occurrence, write the
    modified content back. Two backend calls: cat then write."""
    original = "def hello():\n    print('hi')\n"
    backend = FakeBackend(
        responses=[
            _ok(stdout=original),  # cat returns current file
            _ok(),  # cat > path write succeeds
        ]
    )
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "hello.py",
            "old_str": "print('hi')",
            "new_str": "print('hello')",
        },
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    assert "1 occurrence" in result.payload
    # Two calls: cat then sh -c 'cat > ...'
    assert len(backend.calls) == 2
    assert backend.calls[0]["cmd"][0] == "cat"
    assert backend.calls[1]["cmd"][0] == "sh"
    # Written stdin is the original with the replacement applied.
    written = backend.calls[1]["stdin"].decode("utf-8")
    assert "print('hello')" in written
    assert "print('hi')" not in written


async def test_edit_file_rejects_zero_occurrences(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """old_str not in file → hard error; don't write anything."""
    backend = FakeBackend(responses=[_ok(stdout="unrelated content\n")])
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "x.py",
            "old_str": "never there",
            "new_str": "replacement",
        },
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "not found" in result.payload
    # Only the read happened; no write.
    assert len(backend.calls) == 1


async def test_edit_file_rejects_multiple_occurrences(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """old_str appears multiple times → ambiguous; refuse."""
    backend = FakeBackend(responses=[_ok(stdout="foo\nfoo\nfoo\n")])
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "x.py",
            "old_str": "foo",
            "new_str": "bar",
        },
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "matches 3 places" in result.payload
    assert len(backend.calls) == 1  # no write


async def test_edit_file_empty_old_str(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Empty old_str would match everywhere; reject."""
    backend = FakeBackend()
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "x.py",
            "old_str": "",
            "new_str": "y",
        },
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "non-empty" in result.payload
    assert backend.calls == []


async def test_edit_file_empty_new_str_is_ok(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Empty new_str means 'delete the matched region', a valid op."""
    backend = FakeBackend(
        responses=[
            _ok(stdout="keep\nremove me\nkeep\n"),
            _ok(),
        ]
    )
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "x.py",
            "old_str": "remove me\n",
            "new_str": "",
        },
        _ctx(project_registry, backend),
    )
    assert not result.is_error
    written = backend.calls[1]["stdin"].decode("utf-8")
    assert written == "keep\nkeep\n"


async def test_edit_file_rejects_traversal(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "../../etc/passwd",
            "old_str": "root",
            "new_str": "pwn",
        },
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "Path rejected" in result.payload
    assert backend.calls == []


async def test_edit_file_read_failure_surfaces(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """If the initial read fails (file doesn't exist), no write
    attempt happens."""
    backend = FakeBackend(responses=[_err(1, "cat: nope.py: No such file or directory\n")])
    tool = tool_registry.lookup("edit_file")
    result = await tool.callable(
        {
            "project": "hub",
            "path": "nope.py",
            "old_str": "x",
            "new_str": "y",
        },
        _ctx(project_registry, backend),
    )
    assert result.is_error
    assert "No such file" in result.payload
    assert len(backend.calls) == 1

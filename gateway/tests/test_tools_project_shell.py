"""Tests for ``project_shell`` — Phase 4.7 Task 4.

Covers the shapes the tool's _impl touches:

* schema validation — missing project, missing command, bad
  timeout, out-of-range timeout.
* local-dispatch argv build — uses ``ShellInterpreter.wrap``
  so the argv is ``bash -lc <command>``.
* SSH-dispatch delegation — passes the command string as a
  single argv entry; ``ExecutionBackend`` wraps via the
  remote login shell.
* ``tool_executed`` event emission — success, failure, timeout
  paths each emit one event with the right metadata.
* no-shell-available error path — hub without bash / git-bash
  / wsl surfaces a readable error instead of crashing in the
  subprocess layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gateway.events import EventLog, default_events_path
from gateway.projects import Project, ProjectRegistry
from gateway.tools._types import ToolContext
from gateway.tools.backend import ShellResult
from gateway.tools.local_shell import ShellInterpreter
from gateway.tools.project_shell import build_project_shell_tool

# --------------------------------------------------------------- fakes


class FakeBackend:
    """In-memory stand-in for ExecutionBackend.

    Captures the last ``run_shell`` call's argv + timeout +
    project so tests can assert on them. Returns whatever
    ``ShellResult`` was pre-loaded via :meth:`queue`. One
    result per call; extras raise to catch test overshoot.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[ShellResult] = []

    def queue(self, result: ShellResult) -> None:
        self._queue.append(result)

    async def run_shell(
        self,
        project: Project,
        cmd: list[str],
        *,
        timeout_secs: int,
        **extra: Any,
    ) -> ShellResult:
        self.calls.append(
            {
                "project": project.name,
                "ssh_host": project.ssh_host,
                "cmd": list(cmd),
                "timeout_secs": timeout_secs,
                **extra,
            }
        )
        if not self._queue:
            raise AssertionError(
                "FakeBackend: no more ShellResult queued. Either "
                "the test's .queue() was too short, or the tool "
                "dispatched more rounds than expected."
            )
        return self._queue.pop(0)


def _local_shell_bash() -> ShellInterpreter:
    return ShellInterpreter(label="bash", argv_prefix=("bash", "-lc"), available=True)


def _build_ctx(
    *,
    tmp_path: Path,
    backend: FakeBackend,
    local_shell: ShellInterpreter | None = None,
    events_log: EventLog | None = None,
) -> ToolContext:
    """Build a ToolContext with one ``hub`` project registered.

    Memory, approval, audit — not relevant to project_shell's
    _impl. Leave them None and the tool still works."""
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    (tmp_path / "repo").mkdir(exist_ok=True)
    reg.add(
        Project(
            name="hub",
            ssh_host="",
            path=str(tmp_path / "repo"),
            test_command="pytest -q",
        )
    )
    reg.add(
        Project(
            name="satellite",
            ssh_host="laptop.tailnet",
            path="/home/user/repo",
            test_command="pytest -q",
        )
    )
    return ToolContext(
        client="telegram",
        session_key="main",
        projects=reg,
        backend=backend,
        events=events_log,
        local_shell=local_shell,
    )


# --------------------------------------------------------------- schema


async def test_missing_project_errors(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"command": "ls"}, ctx)
    assert result.is_error
    assert "project" in result.payload.lower()
    assert backend.calls == []


async def test_missing_command_errors(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "hub"}, ctx)
    assert result.is_error
    assert "command" in result.payload.lower()


async def test_empty_command_errors(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "hub", "command": "   "}, ctx)
    assert result.is_error


async def test_timeout_out_of_range_errors(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "hub", "command": "ls", "timeout_secs": 0}, ctx)
    assert result.is_error
    assert "timeout_secs" in result.payload.lower()
    # Too-large is also rejected.
    result = await tool.callable({"project": "hub", "command": "ls", "timeout_secs": 999999}, ctx)
    assert result.is_error


async def test_timeout_wrong_type_errors(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "hub", "command": "ls", "timeout_secs": "fast"}, ctx)
    assert result.is_error


async def test_unknown_project_errors(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "nope", "command": "ls"}, ctx)
    assert result.is_error
    assert "nope" in result.payload


# --------------------------------------------------------------- local dispatch


async def test_local_dispatch_uses_bash_lc(tmp_path: Path) -> None:
    """Local project → argv becomes the shell-interpreter prefix
    plus the command string. The dispatcher passes that to
    ``ExecutionBackend.run_shell``; the backend doesn't add
    further shell wrapping for local projects."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=0, stdout="hi\n", stderr="", timed_out=False))
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())

    result = await tool.callable({"project": "hub", "command": "echo hi | tr a-z A-Z"}, ctx)
    assert not result.is_error
    assert backend.calls == [
        {
            "project": "hub",
            "ssh_host": "",
            "cmd": ["bash", "-lc", "echo hi | tr a-z A-Z"],
            "timeout_secs": 120,
        }
    ]


async def test_local_dispatch_fails_without_shell_interpreter(tmp_path: Path) -> None:
    """No POSIX shell available → readable error; backend never
    invoked. SSH projects are unaffected (tested separately)."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(
        tmp_path=tmp_path,
        backend=backend,
        local_shell=ShellInterpreter.none(),
    )
    result = await tool.callable({"project": "hub", "command": "ls"}, ctx)
    assert result.is_error
    assert "posix shell" in result.payload.lower()
    assert backend.calls == []


async def test_local_dispatch_fails_without_local_shell_on_ctx(tmp_path: Path) -> None:
    """``ctx.local_shell`` is None (older test or misconfigured
    gateway) — same error surface as ``none``."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=None)
    result = await tool.callable({"project": "hub", "command": "ls"}, ctx)
    assert result.is_error
    assert "posix shell" in result.payload.lower()


# --------------------------------------------------------------- SSH dispatch


async def test_ssh_dispatch_wraps_command_in_sh_c(tmp_path: Path) -> None:
    """SSH projects wrap the command in ``sh -c`` before handing
    it to :class:`ExecutionBackend`.

    Why not pass the command as a one-element argv and let
    ``shlex.join`` do the work? Because ``shlex.join([command])``
    on a string with spaces produces a quoted one-word
    expression (``'git pull'``) and the remote login shell
    tries to exec a program named ``"git pull"`` (observed
    2026-05-08 against a Git Bash satellite). ``["sh", "-c",
    command]`` ensures the remote side sees ``sh -c 'the
    whole thing'`` — the familiar and correct shape, matching
    how the ``fitt ssh test`` CLI already handles arbitrary
    shell strings.
    """
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=0, stdout="ok\n", stderr="", timed_out=False))
    # No local_shell on purpose — SSH path doesn't need it.
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=None)

    result = await tool.callable({"project": "satellite", "command": "git pull && uname -a"}, ctx)
    assert not result.is_error
    assert backend.calls == [
        {
            "project": "satellite",
            "ssh_host": "laptop.tailnet",
            "cmd": ["sh", "-c", "git pull && uname -a"],
            "timeout_secs": 120,
        }
    ]


# --------------------------------------------------------------- payload shape


async def test_success_payload_merges_stderr(tmp_path: Path) -> None:
    """Warnings on stderr are appended to the success payload so
    the model can reason about them."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(
        ShellResult(
            exit=0,
            stdout="hello world\n",
            stderr="note: fast-forward only\n",
            timed_out=False,
        )
    )
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "hub", "command": "echo hi"}, ctx)
    assert not result.is_error
    assert "exit=0" in result.payload
    assert "hello world" in result.payload
    assert "--- stderr ---" in result.payload
    assert "fast-forward" in result.payload


async def test_failure_exit_non_zero_returned_as_error(tmp_path: Path) -> None:
    """Non-zero exit → ToolResult.error so the model's
    tool-result handler notices."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(
        ShellResult(
            exit=1,
            stdout="",
            stderr="error: bad thing\n",
            timed_out=False,
        )
    )
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable({"project": "hub", "command": "false"}, ctx)
    assert result.is_error
    assert "exit=1" in result.payload
    assert "bad thing" in result.payload


async def test_timeout_returns_distinct_error(tmp_path: Path) -> None:
    """Timed-out dispatch has a distinct error message so the
    model can differentiate "crashed" from "we killed it."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=-1, stdout="", stderr="", timed_out=True))
    ctx = _build_ctx(tmp_path=tmp_path, backend=backend, local_shell=_local_shell_bash())
    result = await tool.callable(
        {"project": "hub", "command": "sleep 1000", "timeout_secs": 10}, ctx
    )
    assert result.is_error
    assert "timed out" in result.payload.lower()


# --------------------------------------------------------------- events


async def test_success_emits_tool_executed_event(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=0, stdout="ok\n", stderr="", timed_out=False))
    events = EventLog(default_events_path(tmp_path))
    ctx = _build_ctx(
        tmp_path=tmp_path,
        backend=backend,
        local_shell=_local_shell_bash(),
        events_log=events,
    )

    await tool.callable({"project": "hub", "command": "echo hi"}, ctx)

    entries = events.read()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "tool_executed"
    assert entry.session_key == "main"
    assert entry.meta["tool"] == "project_shell"
    assert entry.meta["project"] == "hub"
    assert entry.meta["command"] == "echo hi"
    assert entry.meta["exit_code"] == 0
    assert entry.meta["timed_out"] is False
    assert "duration_ms" in entry.meta
    assert "ok" in entry.body


async def test_failure_emits_event_with_failure_title(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=2, stdout="", stderr="boom\n", timed_out=False))
    events = EventLog(default_events_path(tmp_path))
    ctx = _build_ctx(
        tmp_path=tmp_path,
        backend=backend,
        local_shell=_local_shell_bash(),
        events_log=events,
    )

    await tool.callable({"project": "hub", "command": "false"}, ctx)

    entry = events.read()[-1]
    assert entry.kind == "tool_executed"
    assert "FAILED" in entry.title
    assert entry.meta["exit_code"] == 2


async def test_timeout_emits_event_with_timeout_marker(tmp_path: Path) -> None:
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=-1, stdout="", stderr="", timed_out=True))
    events = EventLog(default_events_path(tmp_path))
    ctx = _build_ctx(
        tmp_path=tmp_path,
        backend=backend,
        local_shell=_local_shell_bash(),
        events_log=events,
    )

    await tool.callable({"project": "hub", "command": "sleep 1000", "timeout_secs": 5}, ctx)

    entry = events.read()[-1]
    assert entry.kind == "tool_executed"
    assert "TIMED OUT" in entry.title
    assert entry.meta["timed_out"] is True


async def test_long_command_truncated_in_event_meta(tmp_path: Path) -> None:
    """Event meta's ``command`` field is capped at 1000 chars —
    the full thing appears in the approval prompt (separate
    path) but carrying 10KB-per-call in the event log would
    balloon it."""
    tool = build_project_shell_tool()
    backend = FakeBackend()
    backend.queue(ShellResult(exit=0, stdout="", stderr="", timed_out=False))
    events = EventLog(default_events_path(tmp_path))
    ctx = _build_ctx(
        tmp_path=tmp_path,
        backend=backend,
        local_shell=_local_shell_bash(),
        events_log=events,
    )
    long_cmd = "echo " + "x" * 5000

    await tool.callable({"project": "hub", "command": long_cmd}, ctx)
    entry = events.read()[-1]
    assert len(entry.meta["command"]) <= 1000
    assert entry.meta["command"].endswith("...")


# --------------------------------------------------------------- shell hook


def test_shell_command_for_returns_command() -> None:
    """The hook is what the approval middleware reads for the
    deny-list check. Must return the command string so the
    deny list actually runs."""
    tool = build_project_shell_tool()
    assert tool.shell_command_for is not None
    assert tool.shell_command_for({"command": "rm -rf /"}) == "rm -rf /"


def test_shell_command_for_tolerates_missing_command() -> None:
    """Missing arg → None (not a crash). Schema validation at
    _impl time returns an error before the hook matters, but
    the hook runs in approval middleware first so it must be
    defensive."""
    tool = build_project_shell_tool()
    assert tool.shell_command_for is not None
    assert tool.shell_command_for({}) is None

"""Tests for the SSH execution backend.

Mocks ``asyncio.create_subprocess_exec`` so we can assert on the
argv the backend builds, without spawning a real ``ssh`` or local
process. Timeout behaviour is tested with a small real sleep so
the ``asyncio.wait_for`` code path is exercised.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.projects import Project
from gateway.tools.backend import ExecutionBackend, ShellResult

# --------------------------------------------------------------- helpers


def _local_project(path: str = "/hub/repo") -> Project:
    return Project(
        name="hub-local",
        ssh_host="",
        path=path,
        test_command="pytest -q",
    )


def _ssh_project(host: str = "laptop.tailnet", path: str = "/home/fred/code/x") -> Project:
    return Project(
        name="remote",
        ssh_host=host,
        path=path,
        test_command="pytest -q",
    )


def _fake_proc(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> Any:
    """Build a mock that matches the shape ``create_subprocess_exec`` returns."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    proc.returncode = returncode
    return proc


# --------------------------------------------------------------- argv building


async def test_local_runs_subprocess_in_project_path() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nonexistent"))
    project = _local_project("/hub/repo")
    proc = _fake_proc(stdout=b"hello\n")
    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as spawn:
        result = await backend.run_shell(project, ["echo", "hello"])
    assert result == ShellResult(exit=0, stdout="hello\n", stderr="", timed_out=False)
    assert result.ok is True
    # First positional args are the argv; kwarg cwd is /hub/repo
    call = spawn.await_args
    assert call is not None
    assert call.args == ("echo", "hello")
    # Windows represents /hub/repo as \hub\repo; normalise.
    assert call.kwargs["cwd"].replace("\\", "/") == "/hub/repo"


async def test_local_cwd_override_is_relative_to_project() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nonexistent"))
    project = _local_project("/hub/repo")
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["pwd"], cwd="sub/dir")
    assert spawn.await_args is not None
    cwd = spawn.await_args.kwargs["cwd"]
    assert cwd.replace("\\", "/") == "/hub/repo/sub/dir"


async def test_local_absolute_cwd_kept_verbatim() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nonexistent"))
    project = _local_project("/hub/repo")
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["ls"], cwd="/elsewhere")
    assert spawn.await_args is not None
    cwd = spawn.await_args.kwargs["cwd"]
    assert cwd.replace("\\", "/") == "/elsewhere"


async def test_ssh_wraps_command_with_cd() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nonexistent"))
    project = _ssh_project("laptop.tailnet", "/home/fred/code/x")
    proc = _fake_proc(stdout=b"hi\n")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["echo", "hi"])
    call = spawn.await_args
    assert call is not None
    argv = call.args
    assert argv[0] == "ssh"
    assert "-o" in argv
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    # Host is the penultimate item before the remote command.
    assert "laptop.tailnet" in argv
    # Remote command is the last positional; verify cd + echo is there.
    remote = argv[-1]
    assert "cd /home/fred/code/x" in remote
    assert remote.endswith("echo hi")
    # cwd for the local ssh process should be None.
    assert call.kwargs["cwd"] is None


async def test_ssh_uses_key_when_file_exists(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("x")
    backend = ExecutionBackend(ssh_key_path=key)
    project = _ssh_project()
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["true"])
    argv = spawn.await_args.args
    assert "-i" in argv
    idx = argv.index("-i")
    assert argv[idx + 1] == str(key)


async def test_ssh_omits_key_arg_when_file_missing() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/definitely-not-there"))
    project = _ssh_project()
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["true"])
    assert "-i" not in spawn.await_args.args


async def test_ssh_relative_cwd_joined_to_project_path() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _ssh_project("host", "/home/user/proj")
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["pytest"], cwd="src/tests")
    remote = spawn.await_args.args[-1]
    assert "cd /home/user/proj/src/tests" in remote


async def test_ssh_absolute_cwd_kept_verbatim() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _ssh_project("host", "/home/user/proj")
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["ls"], cwd="/elsewhere")
    remote = spawn.await_args.args[-1]
    assert "cd /elsewhere" in remote


async def test_ssh_quotes_args_with_spaces() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _ssh_project()
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["grep", "hello world", "file.txt"])
    remote = spawn.await_args.args[-1]
    # shlex.join produces single-quoted argv for tokens that need it.
    assert "'hello world'" in remote


# --------------------------------------------------------------- return values


async def test_returncode_and_stderr_propagated() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _local_project()
    proc = _fake_proc(stdout=b"", stderr=b"nope\n", returncode=2)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await backend.run_shell(project, ["false"])
    assert result.exit == 2
    assert result.stderr == "nope\n"
    assert result.ok is False
    assert result.timed_out is False


async def test_non_utf8_output_is_replaced_not_raised() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _local_project()
    # 0x80 is not a valid UTF-8 lead byte.
    proc = _fake_proc(stdout=b"good\x80bad", stderr=b"")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await backend.run_shell(project, ["weird"])
    assert "good" in result.stdout
    assert result.exit == 0


async def test_none_returncode_coerced_to_minus_one() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _local_project()
    proc = _fake_proc()
    proc.returncode = None
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await backend.run_shell(project, ["sleep", "0"])
    assert result.exit == -1


# --------------------------------------------------------------- timeout


async def test_timeout_kills_process_and_returns_timed_out() -> None:
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _local_project()

    # A proc whose communicate() never completes within the timeout.
    class _StuckProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)  # longer than our wait_for
            return (b"", b"")

        async def wait(self) -> int:
            return -9

        def kill(self) -> None:
            nonlocal killed
            killed = True

    killed = False
    stuck = _StuckProc()

    with patch(
        "asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=stuck),
    ):
        result = await backend.run_shell(project, ["sleep", "10"], timeout_secs=0)

    assert result.timed_out is True
    assert result.exit == -1
    assert "timed out after 0s" in result.stderr
    assert killed is True


# --------------------------------------------------------------- env


async def test_extra_env_overlays_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRE_EXISTING", "from-outer")
    backend = ExecutionBackend(ssh_key_path=Path("/nope"))
    project = _local_project()
    proc = _fake_proc()
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        await backend.run_shell(project, ["env"], extra_env={"FITT_RUN": "1"})
    env = spawn.await_args.kwargs["env"]
    assert env["PRE_EXISTING"] == "from-outer"
    assert env["FITT_RUN"] == "1"


# --------------------------------------------------------------- key discovery


def test_fitt_home_key_autodiscovered(tmp_path: Path) -> None:
    (tmp_path / "ssh").mkdir()
    key = tmp_path / "ssh" / "id_ed25519"
    key.write_text("dummy")
    backend = ExecutionBackend(fitt_home=tmp_path)
    assert backend._ssh_key_path == key


def test_fitt_home_no_key_means_no_key(tmp_path: Path) -> None:
    backend = ExecutionBackend(fitt_home=tmp_path)
    assert backend._ssh_key_path is None

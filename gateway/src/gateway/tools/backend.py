"""Execution backend: local vs SSH dispatch.

Every tool that needs to run a shell command or touch a file on
the execution host goes through ``ExecutionBackend``. The backend
inspects the project's ``ssh_host`` field and either:

* **hub-local**: ``asyncio.create_subprocess_exec`` in the project
  directory; or
* **remote**: wraps the same argv inside
  ``ssh <host> 'cd <cwd> && <cmd>'`` and runs *that* via
  ``create_subprocess_exec`` on the hub.

A tool never has to know which branch applies. Even more
importantly, the spec files and path-resolution rules stay the
same in both cases: the tool computes a path relative to the
project root, and the backend decides how to reach it.

SSH auth (Task 5c):
    The gateway uses key-based SSH only. Two paths are supported
    so the user can pick:

    1. **SSH agent (recommended).** Set ``SSH_AUTH_SOCK`` in the
       gateway's environment (in Docker Compose: bind-mount the
       host's agent socket into the container). The backend
       inherits the env var via ``os.environ``; no extra args are
       needed.

    2. **Key file.** If ``$FITT_HOME/ssh/id_ed25519`` exists, the
       backend passes ``-i <path>`` to every ``ssh`` invocation.
       File must be chmod 0600 on POSIX; on Windows, store in a
       user-only ACL'd folder (default ``%USERPROFILE%\\.fitt\\``).

    The backend also always passes
    ``-o BatchMode=yes -o StrictHostKeyChecking=accept-new`` so
    the gateway never hangs on a password prompt and auto-trusts
    new hosts on first contact (user's tailnet is trusted by
    construction).

Timeouts default to 300 seconds. On timeout we kill the
subprocess and return a ``ShellResult(exit=-1, timed_out=True)``
so callers can distinguish a hung command from a legitimate
non-zero exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..projects import Project

_log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 300
"""Long enough for `run_tests` on most projects; short enough that
a truly wedged command doesn't rot forever. Tools can override."""


# --------------------------------------------------------------- result type


@dataclass(frozen=True, slots=True)
class ShellResult:
    """Outcome of a single ``run_shell`` call.

    ``timed_out=True`` implies the process didn't exit on its own.
    We set ``exit=-1`` in that case so callers can pattern-match.
    ``exit=0`` + ``timed_out=False`` is the only "success" shape.
    """

    exit: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit == 0 and not self.timed_out


# --------------------------------------------------------------- backend


class ExecutionBackend:
    """Run commands on a project's execution host.

    One instance per gateway process. Stateless apart from the
    resolved SSH key path, which is computed once at construction
    (following ``FITT_HOME``) and reused for every remote call.
    """

    def __init__(
        self,
        *,
        fitt_home: Path | None = None,
        ssh_key_path: Path | None = None,
    ) -> None:
        # Explicit key path wins over ``fitt_home`` discovery,
        # which in turn wins over the ``FITT_HOME`` env var.
        if ssh_key_path is not None:
            self._ssh_key_path = ssh_key_path if ssh_key_path.exists() else None
        else:
            home = fitt_home or Path(os.environ.get("FITT_HOME", Path.home() / ".fitt"))
            candidate = home / "ssh" / "id_ed25519"
            self._ssh_key_path = candidate if candidate.exists() else None

    # ---------------------------------------------- shell

    async def run_shell(
        self,
        project: Project,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_secs: int = _DEFAULT_TIMEOUT_S,
        extra_env: dict[str, str] | None = None,
    ) -> ShellResult:
        """Run ``cmd`` on the project's execution host.

        ``cwd`` is resolved relative to ``project.path`` (the
        execution host's copy, not the hub's). When ``project.ssh_host``
        is non-empty the command is wrapped in
        ``ssh <host> 'cd <cwd> && <cmd>'``; when empty the command
        runs locally with ``cwd`` as the working directory.

        The result's ``stdout``/``stderr`` are UTF-8 decoded with
        ``errors="replace"`` so a binary blob in stderr can't crash
        the gateway.
        """
        if project.ssh_host:
            argv, local_cwd = self._build_ssh_argv(project, cmd, cwd)
        else:
            argv, local_cwd = self._build_local_argv(project, cmd, cwd)

        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)

        _log.info(
            "backend.run_shell",
            extra={
                "ssh_host": project.ssh_host or None,
                "argv": argv,
                "cwd": local_cwd,
                "timeout_secs": timeout_secs,
            },
        )

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=local_cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except TimeoutError:
            proc.kill()
            # Drain pipes so the child doesn't zombie.
            try:
                await proc.wait()
            except Exception:  # pragma: no cover - defensive
                pass
            return ShellResult(
                exit=-1,
                stdout="",
                stderr=f"command timed out after {timeout_secs}s",
                timed_out=True,
            )
        return ShellResult(
            exit=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            timed_out=False,
        )

    # ---------------------------------------------- argv builders

    def build_ssh_argv(
        self, project: Project, cmd: list[str], cwd: str | None = None
    ) -> tuple[list[str], str | None]:
        """Public helper: return the exact argv that ``run_shell``
        would dispatch for an SSH project. Used by ``fitt ssh test``
        so users can see (and paste into a shell) the invocation
        that's about to run.

        Mirrors ``_build_ssh_argv`` one-to-one; kept as a public
        facade so the private method can evolve without breaking
        the CLI. No side effects — does not spawn a process.
        """
        return self._build_ssh_argv(project, cmd, cwd)

    def _build_local_argv(
        self, project: Project, cmd: list[str], cwd: str | None
    ) -> tuple[list[str], str]:
        """Build argv for hub-local execution."""
        base = Path(project.path)
        if cwd:
            # Treat cwd as relative to project.path unless absolute.
            target = Path(cwd)
            resolved = target if target.is_absolute() else (base / target)
        else:
            resolved = base
        return list(cmd), str(resolved)

    def _build_ssh_argv(
        self, project: Project, cmd: list[str], cwd: str | None
    ) -> tuple[list[str], str | None]:
        """Wrap ``cmd`` in an ``ssh`` invocation to ``project.ssh_host``."""
        remote = shlex.join(cmd)
        if cwd:
            # Remote cwd is relative to project.path on the remote host.
            remote_cwd = cwd if cwd.startswith("/") else f"{project.path.rstrip('/')}/{cwd}"
            remote = f"cd {shlex.quote(remote_cwd)} && {remote}"
        else:
            remote = f"cd {shlex.quote(project.path)} && {remote}"
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if self._ssh_key_path is not None:
            argv.extend(["-i", str(self._ssh_key_path)])
        argv.extend([project.ssh_host, remote])
        # Local cwd for an SSH invocation is irrelevant; ssh itself
        # has no concept of remote cwd beyond the `cd` we embed.
        return argv, None

"""Phase 4.7 — local POSIX-shell interpreter discovery.

The Phase 4 tools all used argv-isolated dispatch: no shell
string ever reached a process boundary. ``project_shell``
breaks that rule on purpose — the whole point is to run a
user-typed command string with pipes, globs, redirection,
and chaining. That needs a POSIX shell.

On Linux and macOS hubs, ``bash -lc <command>`` is the
answer. On Windows hubs it's more interesting:

* **Git Bash.** Ships as ``C:\\Program Files\\Git\\bin\\bash.exe``
  with its own mini-MSYS env. Good default because most
  developer boxes have it.
* **WSL.** ``wsl -- bash -lc <command>``. Works when a distro
  is installed and registered.
* **Nothing.** A bare Windows hub with no Git and no WSL —
  PowerShell isn't POSIX enough to run the commands the model
  will emit. The probe reports ``none`` so ``project_shell``
  fails cleanly on first call with a readable error.

Why probe at boot rather than at first invocation:

1. **Fail loud.** Surface the "no shell available" condition
   at startup time so it shows in the logs the operator
   reads, not on a live request.
2. **Cache.** The probe runs small subprocesses; doing it
   once per process avoids the latency tax on every shell
   tool call.
3. **Test isolation.** A single ``app.state.local_shell``
   entry means tests can inject a fake without monkeypatching
   the subprocess module deep in the call tree.

Design notes
------------

* **Probe payload is ``echo probe``.** The command is short,
  non-destructive, and every POSIX shell runs it the same way.
  The probe is satisfied only when the subprocess exits 0 AND
  stdout contains ``probe``. The second check rules out a
  shell that writes an unrelated login banner and swallows
  stdin — WSL distros that aren't registered do exactly that.
* **Probe returns the first success.** Ordering: native
  ``bash``, then Git Bash, then WSL. "Native ``bash`` first"
  is the right default on Linux/macOS and on Windows with
  ``bash.exe`` on PATH; Git Bash is the most common fallback;
  WSL is last because it's slow to spawn.
* **``asyncio.subprocess.create_subprocess_exec``.** Matches
  the rest of the tools subsystem (``ExecutionBackend`` uses
  the same API) so testing monkeypatches stay consistent.
* **No env copy.** The probe inherits the gateway's env; a
  ``PATH`` override would silently change which bash wins,
  which is not what we want. The operator's env is what the
  tool will actually run against.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

_log = logging.getLogger(__name__)

# Candidate invocations, in priority order. Each entry is
# ``(label, argv_prefix)`` — the prefix is prepended to the
# command-string argument at dispatch time. The label is what
# we log and attach to the ShellInterpreter.
_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("bash", ("bash", "-lc")),
    ("git-bash", (r"C:\Program Files\Git\bin\bash.exe", "-lc")),
    ("wsl", ("wsl", "--", "bash", "-lc")),
)

_PROBE_COMMAND = "echo probe"
_PROBE_EXPECTED = "probe"
_PROBE_TIMEOUT_S = 3.0


@dataclass(frozen=True, slots=True)
class ShellInterpreter:
    """Resolution result for the local shell probe.

    ``argv_prefix`` is what callers prepend to the command
    string at dispatch time; e.g. on Linux ``["bash", "-lc"]``
    + ``["ls -la | head"]`` → ``bash -lc "ls -la | head"``.

    ``label`` is the short tag we log and show in diagnostics:
    ``bash`` / ``git-bash`` / ``wsl`` / ``none``.

    ``available`` is a convenience bool — false means no POSIX
    shell was found and ``project_shell``'s local path must
    fail with a readable error. SSH projects still work
    because the remote login shell handles the dispatch
    regardless of what's installed on the hub.
    """

    label: str
    argv_prefix: tuple[str, ...]
    available: bool

    @classmethod
    def none(cls) -> ShellInterpreter:
        return cls(label="none", argv_prefix=(), available=False)

    def wrap(self, command: str) -> list[str]:
        """Build the full argv for running ``command``.

        Raises :class:`RuntimeError` when the interpreter is
        unavailable — callers should catch and surface a
        readable error to the model. The alternative (returning
        an empty list) would silently blow up further down the
        call stack.
        """
        if not self.available:
            raise RuntimeError(
                "no POSIX shell interpreter available on this hub. "
                "Install Git Bash or enable WSL to run `project_shell` "
                "against local projects. SSH-backed projects are "
                "unaffected."
            )
        return [*self.argv_prefix, command]


class LocalShellProbe:
    """Detect the local POSIX shell interpreter at gateway boot.

    One instance per gateway process. :meth:`detect` is
    idempotent — repeat calls return the cached result without
    re-spawning subprocesses. Tests pass a fake by constructing
    a ``LocalShellProbe`` with a preset interpreter.
    """

    def __init__(
        self,
        *,
        preset: ShellInterpreter | None = None,
    ) -> None:
        self._cached: ShellInterpreter | None = preset

    async def detect(self) -> ShellInterpreter:
        if self._cached is not None:
            return self._cached
        for label, prefix in _CANDIDATES:
            if await self._works(prefix):
                resolved = ShellInterpreter(
                    label=label,
                    argv_prefix=prefix,
                    available=True,
                )
                self._cached = resolved
                _log.info(
                    "shell.interpreter_resolved",
                    extra={"label": label, "argv_prefix": list(prefix)},
                )
                return resolved
        _log.warning(
            "shell.interpreter_unavailable",
            extra={
                "hint": (
                    "no POSIX shell found on the hub. project_shell "
                    "will fail on local invocations until bash or WSL "
                    "is installed. SSH-backed projects are unaffected."
                )
            },
        )
        self._cached = ShellInterpreter.none()
        return self._cached

    # ------------------------------------------------ internals

    async def _works(self, prefix: tuple[str, ...]) -> bool:
        """Run ``echo probe`` under ``prefix`` and check the output.

        A candidate is "working" only when all three hold:
        - the subprocess actually starts (not FileNotFoundError).
        - it exits 0 within ``_PROBE_TIMEOUT_S`` seconds.
        - its stdout contains ``probe``. The stdout check is
          what rules out WSL registrations that print a
          not-installed banner and exit 0 anyway.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *prefix,
                _PROBE_COMMAND,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as e:
            _log.debug(
                "shell.candidate_missing",
                extra={"argv_prefix": list(prefix), "error": str(e)},
            )
            return False
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # pragma: no cover - defensive
                pass
            return False
        if proc.returncode != 0:
            return False
        return _PROBE_EXPECTED in stdout_b.decode("utf-8", errors="replace")

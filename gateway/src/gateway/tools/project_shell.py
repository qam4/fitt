"""Phase 4.7 — ``project_shell`` inline tool.

One tool, one shape. ``project_shell(project, command,
timeout_secs?)`` executes ``command`` as a shell string on the
project's execution host. Local projects go through
``bash -lc``; SSH projects use the existing ``ExecutionBackend``
wrapper (``ssh host 'cd <path> && <cmd>'``).

Guardrails that already existed in Phase 4 compose in:

* **Deny list.** ``shell_command_for=lambda args: args["command"]``
  tells the approval middleware to run the deny-list check
  before bucket resolution. No new wiring; the hook has been
  sitting on :class:`Tool` since Phase 4 waiting for a
  consumer.
* **Per-tool-per-client defaults.** Registered with
  ``{cli: ask, telegram: ask, ide: ask, webui: block}`` — the
  operator's ``tools.per_client`` block in config.yaml still
  overrides.
* **Approval prompt.** ``approval._summarise_args`` special-cases
  ``project_shell`` to show the full command up to 1000 chars
  (widened from the default 200) so the user sees what they're
  approving.
* **Audit log.** Already captures every tool call, HMAC-chained.

This module adds one new thing: the ``tool_executed`` event.
After a dispatch completes (regardless of exit code), we append
an event so ``fitt inbox`` and the Telegram push see the
invocation. A rejected approval — deny-list-blocked or user-
rejected — doesn't emit the event (the audit log has the
forensic trail; the event stream stays matched to "things that
actually happened on the box").

See ``.kiro/specs/phase4.7-project-shell/design.md`` for the
threat model.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from ..events import EventLog, new_entry
from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from ..projects import Project

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- constants

_DEFAULT_TIMEOUT_S = 120
"""Two minutes by default. Long enough for ``npm install`` on a
modest repo or ``git fetch`` over a slow link; short enough that
a mistyped interactive command doesn't sit forever. Callers
override per-invocation via ``timeout_secs``."""

_MAX_TIMEOUT_S = 1800
"""Thirty-minute ceiling. Past this, a tool call is occupying
the approval thread long enough that it should be a cron, not a
chat-turn tool call."""

_OUTPUT_CAP_BYTES = 64_000
"""Cap stdout/stderr included in the ``ToolResult`` payload. The
event body gets its own cap (``events.telegram_body_cap``);
this cap is specifically so the model's next context window
doesn't balloon when the command dumped a large log."""


# --------------------------------------------------------------- schema

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": {
            "type": "string",
            "description": ("Name of a registered project (see `fitt project list`)."),
        },
        "command": {
            "type": "string",
            "description": (
                "Shell command to execute. Runs under `bash -lc` "
                "locally or via `ssh host 'cd path && <cmd>'` for "
                "SSH projects, so pipes, globs, redirection, and "
                "`&&`/`||` chaining all work. Do NOT use "
                "interactive commands (vim, sudo with prompt); "
                "they'll hang until timeout. Do NOT use trailing "
                "`&` — we wait for stdout/stderr EOF, so a "
                "backgrounded process keeps the tool blocked for "
                "the full timeout."
            ),
        },
        "timeout_secs": {
            "type": "integer",
            "minimum": 1,
            "maximum": _MAX_TIMEOUT_S,
            "default": _DEFAULT_TIMEOUT_S,
            "description": (
                f"Seconds before the command is killed "
                f"(default {_DEFAULT_TIMEOUT_S}, max "
                f"{_MAX_TIMEOUT_S}). Pick shorter for "
                f"read-only probes; longer for builds and "
                f"installs."
            ),
        },
    },
    "required": ["project", "command"],
    "additionalProperties": False,
}


_DESCRIPTION = (
    "Execute a shell command in a registered project. The "
    "command runs under bash -lc locally or via ssh on "
    "satellites. Pipes, globs, redirection, and command "
    "chaining all work. Interactive commands (vim, sudo) hang "
    "until timeout — do not use them. Background processes "
    "(trailing &) block the tool on their stdout; use a cron "
    "or a wrapper script if you want fire-and-forget."
)


# --------------------------------------------------------------- implementation


def _truncate(value: str, cap: int, label: str) -> str:
    if len(value) <= cap:
        return value
    return value[:cap] + (
        f"\n\n... ({len(value) - cap} more bytes truncated; narrow your {label} to see the rest)"
    )


def _summarise_command(command: str, limit: int = 60) -> str:
    """Short command preview for the event title. Collapses
    whitespace so a multi-line heredoc still renders as one
    line in Telegram."""
    compact = " ".join(command.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


async def _impl(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    project_name = args.get("project")
    command = args.get("command")
    timeout_secs_raw = args.get("timeout_secs", _DEFAULT_TIMEOUT_S)

    if not isinstance(project_name, str) or not project_name:
        return ToolResult.error("Missing required argument: project")
    if not isinstance(command, str) or not command.strip():
        return ToolResult.error("Missing required argument: command")
    try:
        timeout_secs = int(timeout_secs_raw)
    except (TypeError, ValueError):
        return ToolResult.error("timeout_secs must be an integer")
    if timeout_secs < 1 or timeout_secs > _MAX_TIMEOUT_S:
        return ToolResult.error(f"timeout_secs must be between 1 and {_MAX_TIMEOUT_S}")

    project: Project
    try:
        project = ctx.projects.get(project_name)
    except Exception as exc:
        return ToolResult.error(f"Unknown project: {project_name} ({exc})")

    if ctx.backend is None:
        return ToolResult.error(
            "Internal error: no execution backend is wired onto "
            "the tool context. This is a gateway bug."
        )

    # Local projects need a POSIX-shell wrapper because
    # ``asyncio.create_subprocess_exec`` doesn't go through a
    # shell (no pipes / redirection / globs without one). SSH
    # projects wrap in ``sh -c`` before handing to
    # ``ExecutionBackend`` — without that wrapper, ``shlex.join``
    # over a one-element list containing a shell string like
    # ``"git pull"`` renders the remote command as the quoted
    # one-word ``'git pull'``, which the remote login shell
    # tries to exec as a program literally named ``git pull``
    # and fails with ``command not found`` (observed 2026-05-08
    # against a Git-Bash-on-Windows satellite). Matches the
    # shape ``fitt ssh test`` uses in the CLI for the same
    # reason.
    if project.ssh_host:
        argv = ["sh", "-c", command]
    else:
        if ctx.local_shell is None or not getattr(ctx.local_shell, "available", False):
            return ToolResult.error(
                "No POSIX shell available on this hub. Install "
                "Git Bash or enable WSL to run `project_shell` "
                "against local projects. SSH-backed projects "
                "are unaffected."
            )
        argv = list(ctx.local_shell.wrap(command))

    started_ts = time.time()
    result = await ctx.backend.run_shell(project, argv, timeout_secs=timeout_secs)
    duration_ms = int((time.time() - started_ts) * 1000)

    # Emit the user-facing event regardless of exit code — a
    # tool that ran matters to the user whether it succeeded or
    # failed. Rejected approvals don't reach this path; that's
    # by design (see design.md "Events mirror execution, not
    # intent").
    events_log = ctx.events
    if isinstance(events_log, EventLog):
        _emit_tool_executed(
            events_log,
            session_key=ctx.session_key,
            project=project.name,
            command=command,
            exit_code=result.exit,
            duration_ms=duration_ms,
            timed_out=result.timed_out,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    # Timeout gets a distinct error shape so the model doesn't
    # confuse "command crashed with exit != 0" with "we killed
    # it after N seconds."
    if result.timed_out:
        return ToolResult.error(
            f"command timed out after {timeout_secs}s (command: {_summarise_command(command)!r})"
        )

    # Merge stdout/stderr into one payload so the model can
    # reason about both. Same pattern as run_tests.
    merged = result.stdout
    if result.stderr:
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += "--- stderr ---\n" + result.stderr

    prefix = f"exit={result.exit}\n\n"
    payload = prefix + merged
    capped = _truncate(payload, _OUTPUT_CAP_BYTES, "command")

    if result.exit == 0:
        return ToolResult.ok(capped)
    # Non-zero exit: return as an error so the model's tool-
    # result handling knows to pay attention. Body still
    # carries the full output so the model can reason about
    # the failure.
    return ToolResult.error(capped)


def _emit_tool_executed(
    events: EventLog,
    *,
    session_key: str,
    project: str,
    command: str,
    exit_code: int,
    duration_ms: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
) -> None:
    """Append the ``tool_executed`` event. Body merges stdout +
    stderr with a separator so operators reading ``fitt inbox``
    see both streams.

    Command is capped at 1000 chars in the meta dict — a 10KB
    shell command is a prompt-injection smell and we don't want
    the event log carrying 10KB-per-call. The approval prompt
    already showed the full string up to the same cap (that's
    what the user approved)."""
    command_for_meta = command if len(command) <= 1000 else command[:997] + "..."
    body_parts: list[str] = []
    if stdout:
        body_parts.append(stdout.rstrip())
    if stderr:
        body_parts.append("--- stderr ---\n" + stderr.rstrip())
    body = "\n".join(body_parts) if body_parts else "(no output)"

    title = f"ran project_shell on {project}: {_summarise_command(command, 30)}"
    if timed_out:
        title = f"TIMED OUT: project_shell on {project}: {_summarise_command(command, 30)}"
    elif exit_code != 0:
        title = (
            f"FAILED (exit={exit_code}): project_shell on "
            f"{project}: {_summarise_command(command, 30)}"
        )

    events.append(
        new_entry(
            kind="tool_executed",
            session_key=session_key,
            title=title,
            body=body,
            meta={
                "tool": "project_shell",
                "project": project,
                "command": command_for_meta,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "timed_out": timed_out,
            },
        )
    )


# --------------------------------------------------------------- factory


def build_project_shell_tool() -> Tool:
    """Return the single ``project_shell`` tool.

    Registered by ``create_app``. No constructor arguments:
    everything it needs (backend, events, local_shell) flows
    via :class:`ToolContext`. That keeps the tool stateless
    and makes unit tests trivial — build a fake context and
    call the implementation.
    """
    return Tool(
        name="project_shell",
        description=_DESCRIPTION,
        schema=_SCHEMA,
        callable=_impl,
        default_bucket=ApprovalBucket.ASK,
        requires_project=True,
        shell_command_for=lambda args: (
            args.get("command") if isinstance(args.get("command"), str) else None
        ),
    )

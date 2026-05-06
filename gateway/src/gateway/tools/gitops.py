"""Git tools that go through the SSH-aware execution backend.

Read-only tools land here first (Task 7): ``git_status`` and
``git_diff``. Task 10 adds ``git_commit`` (a write behind the
``ask`` bucket by default).

All tools dispatch through :class:`~gateway.tools.backend.ExecutionBackend`
so they work both hub-local and via ``ssh <host> 'cd ... && git ...'``
against a project's ``ssh_host``. The commands themselves are plain
``git`` — we don't invoke anything the user couldn't run by hand,
which keeps the behaviour auditable and obvious in the audit log
(Task 13).

Size caps mirror the read-only file tools in ``fileops.py``:
64 KB for status and diff output. A chatty status or a multi-megabyte
diff already exceeds the model's context window, so truncating is a
feature - the tool prompts the caller to narrow the query.
"""

from __future__ import annotations

from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

# --------------------------------------------------------------- caps / timeouts

_STATUS_CAP_BYTES = 64_000
_DIFF_CAP_BYTES = 64_000
_COMMIT_CAP_BYTES = 8_000

_STATUS_TIMEOUT = 30
_DIFF_TIMEOUT = 60
_COMMIT_TIMEOUT = 60

# --------------------------------------------------------------- schemas

_PROJECT_ARG = {
    "type": "string",
    "description": "Name of a registered project (see `fitt project list`).",
}

_SCHEMA_GIT_STATUS: dict[str, Any] = {
    "type": "object",
    "properties": {"project": _PROJECT_ARG},
    "required": ["project"],
    "additionalProperties": False,
}

_SCHEMA_GIT_DIFF: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "ref": {
            "type": "string",
            "description": (
                "Optional git ref or revspec. Omit for working-tree "
                "vs HEAD. Examples: 'HEAD~1', 'main', 'abc123..HEAD'."
            ),
        },
        "path": {
            "type": "string",
            "description": (
                "Optional path or pathspec to limit the diff. Relative to the project root."
            ),
        },
    },
    "required": ["project"],
    "additionalProperties": False,
}

_SCHEMA_GIT_COMMIT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "message": {
            "type": "string",
            "description": (
                "Commit message. Follows git's conventions: the "
                "first line is the subject (≤72 chars), blank line, "
                "then the body. Don't include surrounding quotes; "
                "pass the raw text."
            ),
        },
    },
    "required": ["project", "message"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- helpers


def _resolve_project_for_tool(
    args: dict[str, Any], ctx: ToolContext
) -> tuple[Any, Any] | ToolResult:
    """Return (project, backend) or a ToolResult error.

    Hub-local and ssh projects both valid — the backend takes care
    of the wrap. This mirrors the helper in ``fileops.py`` rather
    than sharing it, so a future refactor of either tool set
    doesn't accidentally couple them.
    """
    project_name = args.get("project")
    if not isinstance(project_name, str) or not project_name:
        return ToolResult.error("Missing required argument: project")
    try:
        project = ctx.projects.get(project_name)
    except Exception as exc:  # UnknownProject
        return ToolResult.error(f"Unknown project: {project_name} ({exc})")
    if ctx.backend is None:
        return ToolResult.error(
            "Internal error: no execution backend is wired onto "
            "the tool context. This is a gateway bug."
        )
    return project, ctx.backend


def _truncate(out: str, cap: int, label: str) -> str:
    if len(out) <= cap:
        return out
    return out[:cap] + (
        f"\n\n... ({len(out) - cap} more bytes truncated; narrow your {label} to see the rest)"
    )


def _validate_ref(ref: str) -> str | None:
    """Reject refs that contain shell-meaningful or traversal chars.

    Returns the ref if it looks safe, or None otherwise. We're
    strict because ``ref`` ends up inside a ``git diff <ref> [--] <path>``
    argv and a too-clever ref could smuggle extra flags. This is
    belt-and-suspenders: the ExecutionBackend uses
    ``create_subprocess_exec`` with a list (no shell interpretation),
    and over ssh it goes through ``shlex.join`` which quotes
    everything. But validating early gives a clean error message
    instead of a baffling git parse failure.
    """
    # Empty is not allowed here — callers should omit the arg
    # entirely for "working tree vs HEAD".
    if not ref:
        return None
    # git accepts a lot of characters. We whitelist the common ones:
    # alnum, '/', '.', '_', '-', '^', '~', '@', '{', '}', and '..'.
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-^~@{}")
    if any(ch not in allowed for ch in ref):
        return None
    # Leading dashes would be parsed as an option by git; reject.
    if ref.startswith("-"):
        return None
    return ref


def _validate_diff_path(path: str) -> str | None:
    """Reject diff pathspecs with '..' segments or leading dashes.

    ``git diff -- <path>`` uses pathspec semantics, which are slightly
    different from regular paths, but the same traversal and
    dash-as-flag concerns apply.
    """
    if not path:
        return None
    if any(seg == ".." for seg in path.replace("\\", "/").split("/")):
        return None
    if path.startswith("-"):
        return None
    return path


# --------------------------------------------------------------- git_status


async def _tool_git_status(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    # `--porcelain=v1` gives machine-readable output that the model
    # can parse consistently; `-b` adds the branch line at the top
    # so the caller sees both "what branch am I on" and "what's
    # modified" in one call. Matches how most users mentally use
    # `git status`.
    result = await backend.run_shell(
        project,
        ["git", "status", "--porcelain=v1", "-b"],
        timeout_secs=_STATUS_TIMEOUT,
    )
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"git status exited {result.exit}").strip())
    out = result.stdout
    if not out.strip():
        # Empty stdout from `git status --porcelain=v1 -b` is
        # theoretically impossible (the -b line is always
        # present), but handle it defensively so we don't return
        # an empty success string that the LLM might misread.
        return ToolResult.ok("(clean working tree; no branch info)")
    return ToolResult.ok(_truncate(out, _STATUS_CAP_BYTES, "repo state"))


# --------------------------------------------------------------- git_diff


async def _tool_git_diff(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    ref = args.get("ref")
    path = args.get("path")
    if ref is not None and not isinstance(ref, str):
        return ToolResult.error("Argument 'ref' must be a string")
    if path is not None and not isinstance(path, str):
        return ToolResult.error("Argument 'path' must be a string")

    cmd: list[str] = ["git", "--no-pager", "diff"]
    if ref:
        safe_ref = _validate_ref(ref)
        if safe_ref is None:
            return ToolResult.error(
                f"ref rejected (disallowed characters or starts with '-'): {ref!r}"
            )
        cmd.append(safe_ref)
    # `--` is git's separator between refs/options and pathspecs.
    # Include it whenever a path is supplied, even if no ref was,
    # so a path like `-main` can't be mistaken for a flag.
    if path:
        safe_path = _validate_diff_path(path)
        if safe_path is None:
            return ToolResult.error(f"path rejected (contains '..' or starts with '-'): {path!r}")
        cmd.append("--")
        cmd.append(safe_path)

    result = await backend.run_shell(project, cmd, timeout_secs=_DIFF_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"git diff exited {result.exit}").strip())
    out = result.stdout
    if not out:
        return ToolResult.ok("(no changes)")
    return ToolResult.ok(_truncate(out, _DIFF_CAP_BYTES, "diff scope"))


# --------------------------------------------------------------- git_commit


async def _tool_git_commit(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Stage all changes and commit with the given message.

    Implied ``git add -A`` so callers don't have to reason about
    staging semantics. If there's nothing to commit, returns a
    clear "(nothing to commit)" rather than an error, so the LLM
    can distinguish "I didn't need to commit" from "commit broke".
    """
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    message = args.get("message")
    if not isinstance(message, str) or not message.strip():
        return ToolResult.error("Argument 'message' must be a non-empty string")
    if len(message.encode("utf-8")) > _COMMIT_CAP_BYTES:
        return ToolResult.error(
            f"commit message exceeds {_COMMIT_CAP_BYTES} bytes. "
            "Shorten it or use a commit-message file workflow outside FITT."
        )

    # `git add -A` stages every change (tracked, untracked, deletions).
    add = await backend.run_shell(project, ["git", "add", "-A"], timeout_secs=_COMMIT_TIMEOUT)
    if add.timed_out:
        return ToolResult.error(add.stderr)
    if add.exit != 0:
        return ToolResult.error((add.stderr or f"git add exited {add.exit}").strip())

    # `--allow-empty-message` isn't set — if the model sends "", we
    # already caught it above. Use `-m` to pass the message as a
    # single argv item; argv is quoted by the backend on SSH, so
    # embedded newlines, quotes, and shell metacharacters all pass
    # through unchanged.
    commit = await backend.run_shell(
        project, ["git", "commit", "-m", message], timeout_secs=_COMMIT_TIMEOUT
    )
    if commit.timed_out:
        return ToolResult.error(commit.stderr)
    if commit.exit == 0:
        return ToolResult.ok(_truncate(commit.stdout.strip(), _DIFF_CAP_BYTES, "commit output"))

    # Distinguish "nothing to commit" from real failures. git prints
    # a message like "nothing to commit, working tree clean" to
    # stdout with exit 1 in that case.
    combined = (commit.stdout + commit.stderr).lower()
    if "nothing to commit" in combined:
        return ToolResult.ok("(nothing to commit; working tree clean)")
    return ToolResult.error(
        (commit.stderr or commit.stdout or f"git commit exited {commit.exit}").strip()
    )


# --------------------------------------------------------------- builder


def build_git_tools() -> list[Tool]:
    """Return the Phase-4-task-7 read-only git tools."""
    return [
        Tool(
            name="git_status",
            description=(
                "Show the current git branch and the porcelain status "
                "of the project (tracked changes, untracked files)."
            ),
            schema=_SCHEMA_GIT_STATUS,
            callable=_tool_git_status,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="git_diff",
            description=(
                "Show `git diff` output. With no ref, diffs the working "
                "tree against HEAD; with a ref, diffs against that ref "
                "or revspec. Optionally scoped to a path."
            ),
            schema=_SCHEMA_GIT_DIFF,
            callable=_tool_git_diff,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="git_commit",
            description=(
                "Stage all changes (implied `git add -A`) and commit "
                "with the given message. Returns '(nothing to commit)' "
                "cleanly when the working tree is already clean."
            ),
            schema=_SCHEMA_GIT_COMMIT,
            callable=_tool_git_commit,
            default_bucket=ApprovalBucket.ASK,
            requires_project=True,
            kind="inline",
        ),
    ]

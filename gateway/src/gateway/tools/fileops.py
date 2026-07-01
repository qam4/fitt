"""File-access tools that go through the SSH-aware execution backend.

Every tool here takes a ``project`` argument and a path (or pattern).
The path is validated against path-traversal first, then handed to
:class:`~gateway.tools.backend.ExecutionBackend` which runs the right
command on the right host — hub-local for projects without an
``ssh_host``, or wrapped in ``ssh`` for remote projects.

The commands chosen (``cat``, ``ls -la``, ``find``, and ``grep -rn``)
are POSIX-portable so they work on any execution host in the
tailnet. Windows satellites aren't a supported tool target in v0;
if the host lacks these binaries (busybox NAS can be thin),
tool calls return a structured error rather than a silent empty
result.

Tools in this module:

* ``read_file(project, path)`` — return file contents or an error.
* ``list_directory(project, path)`` — ``ls -la`` output as text.
* ``grep_repo(project, pattern, path_filter?)`` — grep across the
  project tree, optionally scoped by a glob ``path_filter``.
* ``glob_search(project, pattern)`` — ``find`` with a ``-name``
  pattern.

Size limits: ``read_file`` caps output at 200 KB (matching
``spec_read``). ``list_directory`` and ``grep_repo`` cap at 64 KB
because larger outputs blow out the chat context without adding
signal. Tool callers should narrow queries on follow-ups rather
than paginate, matching how the LLM actually uses these.

Phase 4.10 — built-in ``fitt`` pseudo-project: the read-side
fileops accept ``project=fitt`` as a reserved name resolving to
``$FITT_HOME`` with a hard-coded subdir allowlist. Only entries
in :data:`_FITT_BUILTIN_ALLOWLIST` are reachable; everything
else returns "Path rejected." This is what makes the
``[Skills available]`` recipe-load hint executable
(``read_file project=fitt path=skills/<name>/SKILL.md``) without
exposing secrets, ssh keys, or audit logs to the model. Widening
the allowlist later (e.g. ``sessions/`` for Phase 7's session
search) is a one-line change with deliberate review.
"""

from __future__ import annotations

import difflib
from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

# --------------------------------------------------------------- size caps

# --------------------------------------------------------------- size caps

_READ_CAP_BYTES = 200_000
_LIST_CAP_BYTES = 64_000
_GREP_CAP_BYTES = 64_000
_GLOB_CAP_BYTES = 64_000

_WRITE_CAP_BYTES = 500_000
"""Cap on ``write_file`` / ``edit_file`` output sizes (content the
model is asking us to write). Larger than the read cap because a
programmatic codegen pass may legitimately produce a larger file
than a human would ever read in one go; smaller than 1 MB because
the model's own context wouldn't have coherently produced that
much in a single turn anyway."""

# Timeouts per tool. Read and list are fast; grep and find can
# traverse large trees, so give them more room but still bounded.
_READ_TIMEOUT = 30
_LIST_TIMEOUT = 30
_GREP_TIMEOUT = 120
_GLOB_TIMEOUT = 60
_WRITE_TIMEOUT = 60
_EDIT_TIMEOUT = 60


# --------------------------------------------------------------- schemas

_PROJECT_ARG = {
    "type": "string",
    "description": "Name of a registered project (see `fitt project list`).",
}

_PATH_ARG = {
    "type": "string",
    "description": (
        "Path relative to the project root. Use forward slashes. "
        "'..' is rejected; absolute paths are only accepted when "
        "they're inside the project root."
    ),
}

_SCHEMA_READ_FILE = {
    "type": "object",
    "properties": {"project": _PROJECT_ARG, "path": _PATH_ARG},
    "required": ["project", "path"],
    "additionalProperties": False,
}

_SCHEMA_LIST_DIRECTORY = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "path": {**_PATH_ARG, "default": "."},
    },
    "required": ["project"],
    "additionalProperties": False,
}

_SCHEMA_GREP_REPO = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "pattern": {
            "type": "string",
            "description": (
                "Extended regex pattern passed to `grep -E`. "
                "The project tree is searched recursively."
            ),
        },
        "path_filter": {
            "type": "string",
            "description": (
                "Optional shell glob (e.g. '*.py') to scope the search to matching filenames."
            ),
        },
    },
    "required": ["project", "pattern"],
    "additionalProperties": False,
}

_SCHEMA_GLOB_SEARCH = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "pattern": {
            "type": "string",
            "description": ("Filename glob passed to `find -name`, e.g. '*.md'."),
        },
    },
    "required": ["project", "pattern"],
    "additionalProperties": False,
}

_SCHEMA_WRITE_FILE = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "path": _PATH_ARG,
        "content": {
            "type": "string",
            "description": (
                "Full new contents of the file. Overwrites if the "
                "file exists; creates parent directories as needed. "
                "Use edit_file for a surgical change on a large file."
            ),
        },
    },
    "required": ["project", "path", "content"],
    "additionalProperties": False,
}

_SCHEMA_EDIT_FILE = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "path": _PATH_ARG,
        "old_str": {
            "type": "string",
            "description": (
                "Exact string currently in the file to be replaced. "
                "Must appear exactly once, anywhere in the file. "
                "Include enough context (surrounding lines) for "
                "uniqueness."
            ),
        },
        "new_str": {
            "type": "string",
            "description": (
                "Replacement string. May be empty to delete the "
                "matched region. Whitespace is preserved verbatim."
            ),
        },
    },
    "required": ["project", "path", "old_str", "new_str"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- helpers


# --------------------------------------------------------------- builtin fitt project

_FITT_BUILTIN_NAME = "fitt"
"""Reserved project name for the built-in pseudo-project. The
operator cannot define a project with this name; the resolver
shadows it. Used by the [Skills available] block's recipe-load
hint so the model can call
``read_file project=fitt path=skills/<name>/SKILL.md`` without
operator-side projects.yaml configuration."""

_FITT_BUILTIN_ALLOWLIST = ("skills",)
"""Subdirectories of ``$FITT_HOME`` reachable via the built-in
``fitt`` pseudo-project. Hard-coded; widening is a deliberate
code change. Path validation in ``_resolve_project_for_tool``
rejects anything outside this allowlist. Today: skills only.
Phase 7+ may add ``sessions`` for cross-session search."""


def _maybe_resolve_builtin_fitt_project(
    project_name: str, ctx: ToolContext
) -> tuple[Any, Any] | ToolResult | None:
    """Return ``(project, backend)`` when the call targets the
    built-in ``fitt`` pseudo-project, ``ToolResult`` on a
    rejected path, or ``None`` to fall through to the regular
    ``ProjectRegistry`` lookup.

    This intercepts ``project=fitt`` before
    ``_resolve_project_for_tool`` consults the operator's
    ``projects.yaml`` so the name is reserved by FITT itself.
    A user-defined project named ``fitt`` would be silently
    shadowed by the built-in; that's intentional — the name
    is owned by us.
    """
    if project_name != _FITT_BUILTIN_NAME:
        return None

    # Lazy import: the projects module imports config which
    # imports tools indirectly. Local import keeps the dep graph
    # clean.
    from ..config import fitt_home
    from ..projects import Project

    home = fitt_home()
    fitt_project = Project(
        name=_FITT_BUILTIN_NAME,
        path=str(home),
        ssh_host="",  # always hub-local
    )

    if ctx.backend is None:
        return ToolResult.error(
            "Internal error: no execution backend is wired onto "
            "the tool context. This is a gateway bug."
        )
    return fitt_project, ctx.backend


def _path_is_in_fitt_allowlist(safe_path: str, project_path: str) -> bool:
    """Return True when ``safe_path`` (already passed
    ``_safe_path``'s traversal check against ``project_path``)
    points inside one of the allowlisted FITT_HOME subdirs.

    ``safe_path`` may be relative (``"skills/foo/SKILL.md"``)
    or absolute (``"/fitt-home/skills/foo/SKILL.md"``). We
    normalise to a relative-to-project_path form before checking
    the first segment.
    """
    posix = safe_path.replace("\\", "/")
    if posix.startswith("/"):
        # Absolute path — strip the project_path prefix so we can
        # reason about the subdir suffix only.
        prefix = project_path.rstrip("/") + "/"
        if posix == project_path:
            relative = "."
        elif posix.startswith(prefix):
            relative = posix[len(prefix) :]
        else:
            # Shouldn't happen — _safe_path already checked this.
            return False
    else:
        relative = posix

    if relative in (".", ""):
        # Listing the FITT root itself isn't allowed; force
        # callers to pick an allowlisted subdir.
        return False

    first_segment = relative.split("/", 1)[0]
    return first_segment in _FITT_BUILTIN_ALLOWLIST


def _enforce_fitt_allowlist(project: Any, safe_path: str, original_path: str) -> ToolResult | None:
    """Return a ToolResult error when ``project`` is the built-in
    ``fitt`` pseudo-project and ``safe_path`` falls outside the
    allowlisted subdirs. Returns ``None`` otherwise.

    Read-side fileops call this after ``_safe_path`` to layer
    the FITT-specific subdir restriction on top of the standard
    project-root traversal check.
    """
    if project.name != _FITT_BUILTIN_NAME:
        return None
    if _path_is_in_fitt_allowlist(safe_path, project.path):
        return None
    return ToolResult.error(
        f"Path rejected for built-in 'fitt' project: only "
        f"{', '.join(_FITT_BUILTIN_ALLOWLIST)}/ subdirs are reachable "
        f"(got {original_path!r})"
    )


def _reject_fitt_for_writes(project: Any) -> ToolResult | None:
    """Return a ToolResult error when ``project`` is the built-in
    ``fitt`` pseudo-project. Used by ``write_file`` and
    ``edit_file`` — the built-in is read-only.
    """
    if project.name != _FITT_BUILTIN_NAME:
        return None
    return ToolResult.error(
        "The built-in 'fitt' project is read-only. Edit files under "
        "$FITT_HOME directly on the host or via project_shell."
    )


def _resolve_project_for_tool(
    args: dict[str, Any], ctx: ToolContext
) -> tuple[Any, Any] | ToolResult:
    """Return (project, backend) or a ToolResult error.

    Both hub-local and ssh projects are accepted — that's the
    whole point of the execution backend. What we verify here is
    that the project exists and the backend is wired onto the
    context.

    ``project=fitt`` is reserved for the built-in pseudo-project
    rooted at ``$FITT_HOME``; see
    :func:`_maybe_resolve_builtin_fitt_project`.
    """
    project_name = args.get("project")
    if not isinstance(project_name, str) or not project_name:
        return ToolResult.error("Missing required argument: project")

    builtin = _maybe_resolve_builtin_fitt_project(project_name, ctx)
    if builtin is not None:
        return builtin

    try:
        project = ctx.projects.get(project_name)
    except Exception as exc:
        return ToolResult.error(f"Unknown project: {project_name} ({exc})")
    if ctx.backend is None:
        return ToolResult.error(
            "Internal error: no execution backend is wired onto "
            "the tool context. This is a gateway bug."
        )
    return project, ctx.backend


def _safe_path(path: str, project_path: str) -> str | None:
    """Validate ``path`` against traversal.

    Returns a normalised path (relative to project_path, or an
    absolute path inside project_path) or None if the path is
    unsafe.

    We deliberately stay stringly-typed rather than using
    ``pathlib.Path`` here because the path will be interpreted on
    the execution host, not locally. Local path semantics
    (especially Windows ``\\``) would mislead. POSIX-style
    analysis catches the cases that matter.
    """
    # Empty = project root.
    if path in ("", "."):
        return "."
    # Normalise separators.
    posix = path.replace("\\", "/")
    # Reject raw parent refs regardless of position; cheap and
    # catches the common mistake of "../../etc/passwd" or a
    # tucked-in "foo/../..".
    parts = [p for p in posix.split("/") if p != ""]
    if any(p == ".." for p in parts):
        return None
    # Absolute path: only accept if it begins with project_path.
    if posix.startswith("/"):
        if not posix.startswith(project_path.rstrip("/") + "/") and posix != project_path:
            return None
        return posix
    return "/".join(parts)


def _truncate(out: str, cap: int, label: str) -> str:
    """Cap command output at ``cap`` chars, appending a note."""
    if len(out) <= cap:
        return out
    return out[:cap] + (
        f"\n\n... ({len(out) - cap} more bytes truncated; narrow your {label} to see the rest)"
    )


# --------------------------------------------------------------- read_file


async def _tool_read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    path = args.get("path")
    if not isinstance(path, str) or not path:
        return ToolResult.error("Missing required argument: path")
    safe = _safe_path(path, project.path)
    if safe is None:
        return ToolResult.error(f"Path rejected (escapes project root or uses '..'): {path!r}")

    rejected = _enforce_fitt_allowlist(project, safe, path)
    if rejected is not None:
        return rejected

    # Use `cat` so the same command works hub-local and via ssh.
    result = await backend.run_shell(project, ["cat", "--", safe], timeout_secs=_READ_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        # cat prints "No such file or directory" or similar on
        # stderr; surface that verbatim rather than synthesising.
        return ToolResult.error((result.stderr or f"cat exited {result.exit}").strip())
    return ToolResult.ok(_truncate(result.stdout, _READ_CAP_BYTES, "read_file path"))


# --------------------------------------------------------------- list_directory


async def _tool_list_directory(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    path = args.get("path", ".")
    if not isinstance(path, str):
        return ToolResult.error("Argument 'path' must be a string")
    safe = _safe_path(path, project.path)
    if safe is None:
        return ToolResult.error(f"Path rejected (escapes project root or uses '..'): {path!r}")

    rejected = _enforce_fitt_allowlist(project, safe, path)
    if rejected is not None:
        return rejected

    # `ls -la` is the portable answer (BSD + GNU both support it).
    # `--` guards against paths starting with `-`.
    result = await backend.run_shell(project, ["ls", "-la", "--", safe], timeout_secs=_LIST_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"ls exited {result.exit}").strip())
    return ToolResult.ok(_truncate(result.stdout, _LIST_CAP_BYTES, "list_directory path"))


# --------------------------------------------------------------- grep_repo


async def _tool_grep_repo(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    if project.name == _FITT_BUILTIN_NAME:
        return ToolResult.error(
            "grep_repo is not supported on the built-in 'fitt' project. "
            "Use read_file with a specific path under "
            f"{', '.join(_FITT_BUILTIN_ALLOWLIST)}/ instead."
        )

    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return ToolResult.error("Missing required argument: pattern")
    path_filter = args.get("path_filter")
    if path_filter is not None and not isinstance(path_filter, str):
        return ToolResult.error("Argument 'path_filter' must be a string")

    # Recursive grep, show line numbers, ignore binary files to
    # avoid dumping bytes into the chat context.
    cmd = ["grep", "-rnIE"]
    if path_filter:
        cmd.extend(["--include", path_filter])
    cmd.extend(["--", pattern, "."])

    result = await backend.run_shell(project, cmd, timeout_secs=_GREP_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(result.stderr)
    # grep returns 1 for "no matches". That's a valid outcome, not
    # an error; only >=2 signals real trouble.
    if result.exit >= 2:
        return ToolResult.error((result.stderr or f"grep exited {result.exit}").strip())
    if result.exit == 1:
        return ToolResult.ok("(no matches)")
    return ToolResult.ok(_truncate(result.stdout, _GREP_CAP_BYTES, "pattern"))


# --------------------------------------------------------------- glob_search


async def _tool_glob_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    if project.name == _FITT_BUILTIN_NAME:
        return ToolResult.error(
            "glob_search is not supported on the built-in 'fitt' project. "
            "Use list_directory with a specific path under "
            f"{', '.join(_FITT_BUILTIN_ALLOWLIST)}/ instead."
        )

    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return ToolResult.error("Missing required argument: pattern")

    # `find . -name <pattern>` walks the whole tree. We don't
    # follow symlinks (default) so a loop in the filesystem can't
    # wedge us.
    result = await backend.run_shell(
        project,
        ["find", ".", "-type", "f", "-name", pattern],
        timeout_secs=_GLOB_TIMEOUT,
    )
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"find exited {result.exit}").strip())
    out = result.stdout.strip()
    if not out:
        return ToolResult.ok("(no matches)")
    return ToolResult.ok(_truncate(out, _GLOB_CAP_BYTES, "pattern"))


# --------------------------------------------------------------- write_file


async def _tool_write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Overwrite (or create) a file with the given content.

    Implemented as ``mkdir -p <dir> && cat > <file>`` piped through
    stdin so content with quotes, newlines, or shell metacharacters
    flows through unchanged. Works hub-local and over SSH.
    """
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    rejected = _reject_fitt_for_writes(project)
    if rejected is not None:
        return rejected

    path = args.get("path")
    if not isinstance(path, str) or not path:
        return ToolResult.error("Missing required argument: path")
    content = args.get("content")
    if not isinstance(content, str):
        return ToolResult.error("Missing or non-string argument: content")
    if len(content.encode("utf-8")) > _WRITE_CAP_BYTES:
        return ToolResult.error(
            f"content exceeds {_WRITE_CAP_BYTES} bytes. Split the write "
            "into smaller files, or use edit_file on an existing one."
        )
    safe = _safe_path(path, project.path)
    if safe is None or safe == ".":
        return ToolResult.error(
            f"Path rejected (escapes project root, uses '..', or is the "
            f"project root itself): {path!r}"
        )

    # Compose `mkdir -p $(dirname <path>) && cat > <path>`. Both
    # commands run through sh so the shell handles the redirection;
    # we control `safe` so there's no injection risk (path has been
    # validated by _safe_path and will be shlex-quoted by the
    # ExecutionBackend when it builds the ssh remote command).
    import shlex

    script = f"mkdir -p -- {shlex.quote(_dirname(safe))} && cat > {shlex.quote(safe)}"
    result = await backend.run_shell(
        project,
        ["sh", "-c", script],
        timeout_secs=_WRITE_TIMEOUT,
        stdin=content.encode("utf-8"),
    )
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"write exited {result.exit}").strip())
    return ToolResult.ok(f"wrote {len(content)} chars to {safe}")


def _dirname(path: str) -> str:
    """Return the directory portion of a POSIX-style path.

    Not using ``os.path.dirname`` because the path will be
    interpreted on the execution host which may be Linux while
    the gateway runs on Windows — POSIX semantics are what
    matter, not local OS semantics."""
    if "/" not in path:
        return "."
    head = path.rsplit("/", 1)[0]
    return head if head else "/"


# --------------------------------------------------------------- edit_file


_NEAR_MISS_MAX_CHARS = 600
"""Cap on the near-miss snippet quoted back in the error. Enough to show
the block the model was aiming at without dumping a whole function."""

_NEAR_MISS_MIN_RATIO = 0.6
"""Below this similarity the "closest" region is noise, not a near-miss —
quoting it would mislead more than help, so we stay silent."""


def _find_near_miss(content: str, old_str: str) -> str | None:
    """Return the file region most similar to ``old_str`` (or ``None`` when
    nothing is close enough to be useful).

    The overwhelmingly common ``edit_file`` failure is a whitespace or
    indentation mismatch: the model's ``old_str`` is *nearly* right but a
    tab/space or a trimmed line stops the exact match. Quoting the closest
    on-disk text lets the model see the exact bytes it must reproduce,
    turning a blind retry into a targeted one. Line-windowed
    :class:`difflib.SequenceMatcher` over a size-capped file is cheap."""
    content_lines = content.splitlines()
    needle_lines = old_str.splitlines() or [old_str]
    n = len(needle_lines)
    if not content_lines:
        return None
    needle = "\n".join(needle_lines)
    best_ratio = 0.0
    best_window: list[str] = []
    # Slide an n-line window across the file; if old_str has more lines
    # than the file, compare against the whole file (one window).
    if n >= len(content_lines):
        windows = [content_lines]
    else:
        windows = [content_lines[i : i + n] for i in range(len(content_lines) - n + 1)]
    for window in windows:
        ratio = difflib.SequenceMatcher(None, "\n".join(window), needle).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_window = window
    if not best_window or best_ratio < _NEAR_MISS_MIN_RATIO:
        return None
    snippet = "\n".join(best_window)
    if len(snippet) > _NEAR_MISS_MAX_CHARS:
        snippet = snippet[:_NEAR_MISS_MAX_CHARS] + "…"
    return snippet


def _match_line_numbers(content: str, needle: str, *, limit: int = 5) -> list[int]:
    """1-based line numbers where ``needle`` starts in ``content`` (up to
    ``limit``). Helps the model disambiguate a >1-match failure."""
    lines: list[int] = []
    pos = content.find(needle)
    while pos != -1 and len(lines) < limit:
        lines.append(content.count("\n", 0, pos) + 1)
        pos = content.find(needle, pos + 1)
    return lines


async def _tool_edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Surgical replacement: find ``old_str`` once, replace with
    ``new_str``.

    Refuses if ``old_str`` appears zero times (caller's context is
    stale) or more than once (caller's context is ambiguous and
    the LLM should include more surrounding text). This is the
    tool contract every agent-IDE uses for the same reason: a
    silent multi-replace is a silent bug."""
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    rejected = _reject_fitt_for_writes(project)
    if rejected is not None:
        return rejected

    path = args.get("path")
    if not isinstance(path, str) or not path:
        return ToolResult.error("Missing required argument: path")
    old_str = args.get("old_str")
    new_str = args.get("new_str")
    if not isinstance(old_str, str) or old_str == "":
        return ToolResult.error("Argument 'old_str' must be a non-empty string")
    if not isinstance(new_str, str):
        return ToolResult.error("Argument 'new_str' must be a string (may be empty)")
    safe = _safe_path(path, project.path)
    if safe is None or safe == ".":
        return ToolResult.error(
            f"Path rejected (escapes project root, uses '..', or is the "
            f"project root itself): {path!r}"
        )

    # Read current content via cat.
    read_result = await backend.run_shell(project, ["cat", "--", safe], timeout_secs=_READ_TIMEOUT)
    if read_result.timed_out:
        return ToolResult.error(read_result.stderr)
    if read_result.exit != 0:
        return ToolResult.error((read_result.stderr or f"cat exited {read_result.exit}").strip())
    current = read_result.stdout

    # Validate exactly-one-occurrence.
    count = current.count(old_str)
    if count == 0:
        near = _find_near_miss(current, old_str)
        hint = ""
        if near is not None:
            hint = (
                "\n\nClosest text in the file — your old_str must match this "
                "exactly, including whitespace and indentation:\n"
                f"----\n{near}\n----"
            )
        return ToolResult.error(
            f"old_str not found in {safe}. Re-read the file with read_file "
            "and include enough surrounding context for a unique match." + hint
        )
    if count > 1:
        line_nums = _match_line_numbers(current, old_str)
        where = ""
        if line_nums:
            shown = ", ".join(f"line {n}" for n in line_nums)
            more = " (and more)" if count > len(line_nums) else ""
            where = f" It starts at {shown}{more}."
        return ToolResult.error(
            f"old_str matches {count} places in {safe}.{where} Include more "
            "surrounding context (e.g. the line above or below) so the match "
            "is unique."
        )

    new_content = current.replace(old_str, new_str, 1)
    if len(new_content.encode("utf-8")) > _WRITE_CAP_BYTES:
        return ToolResult.error(
            f"edited content would exceed {_WRITE_CAP_BYTES} bytes. "
            "Consider splitting the file instead."
        )

    # Stream the new content back via cat > path. Parent dir is
    # known to exist because we just read from it.
    import shlex

    script = f"cat > {shlex.quote(safe)}"
    write_result = await backend.run_shell(
        project,
        ["sh", "-c", script],
        timeout_secs=_EDIT_TIMEOUT,
        stdin=new_content.encode("utf-8"),
    )
    if write_result.timed_out:
        return ToolResult.error(write_result.stderr)
    if write_result.exit != 0:
        return ToolResult.error(
            (write_result.stderr or f"write exited {write_result.exit}").strip()
        )
    return ToolResult.ok(f"replaced 1 occurrence in {safe} ({len(old_str)} → {len(new_str)} chars)")


# --------------------------------------------------------------- builder


def build_fileops_tools() -> list[Tool]:
    """Return the Phase-4-task-6 set of file tools."""
    return [
        Tool(
            name="read_file",
            description=(
                "Read a file from a registered project. Works hub-local and over SSH; size-capped."
            ),
            schema=_SCHEMA_READ_FILE,
            callable=_tool_read_file,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="list_directory",
            description=(
                "List files in a directory inside a project "
                "(ls -la). Default path is the project root."
            ),
            schema=_SCHEMA_LIST_DIRECTORY,
            callable=_tool_list_directory,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="grep_repo",
            description=(
                "Recursively grep a project for a pattern, optionally scoped with a --include glob."
            ),
            schema=_SCHEMA_GREP_REPO,
            callable=_tool_grep_repo,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="glob_search",
            description=("Find files in a project by name pattern (find . -name <pattern>)."),
            schema=_SCHEMA_GLOB_SEARCH,
            callable=_tool_glob_search,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="write_file",
            description=(
                "Create or overwrite a file with the given content. "
                "Parent directories are created as needed. Fails if "
                "the path escapes the project root. Prefer edit_file "
                "for surgical changes on large files."
            ),
            schema=_SCHEMA_WRITE_FILE,
            callable=_tool_write_file,
            default_bucket=ApprovalBucket.ASK,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="edit_file",
            description=(
                "Replace an exact substring in a file. Fails if "
                "old_str appears zero or more than once; include "
                "enough surrounding context to make the match unique."
            ),
            schema=_SCHEMA_EDIT_FILE,
            callable=_tool_edit_file,
            default_bucket=ApprovalBucket.ASK,
            requires_project=True,
            kind="inline",
        ),
    ]

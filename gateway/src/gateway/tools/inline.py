"""Inline tool implementations.

"Inline" means the tool runs inside the gateway process, dispatching
file operations through the SSH-aware :class:`ExecutionBackend` when
the project has an ``ssh_host``. Same shape as ``fileops.py`` and
``gitops.py``; the difference is these tools are opinionated about
what they read (spec files under ``.kiro/specs/``) rather than
generic file utilities.

Tools in this module:

- ``list_capabilities``  — describe the registry (hub-local only).
- ``spec_list``          — feature folders under ``.kiro/specs/``.
- ``spec_read``          — concatenated requirements/design/tasks.
- ``spec_next_task``     — first unchecked ``- [ ]`` in tasks.md.
- ``spec_mark_task``     — flip a ``- [ ]`` to ``- [x]`` in tasks.md.

The spec tools POSIX-dispatch everything:
  ``ls``, ``test -f`` + ``cat``, ``cat | grep -nE``, ``mv``. Git Bash
on a Windows satellite provides the same binaries, so the same argv
works against either a hub-local project or a laptop reachable over
SSH.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

# --------------------------------------------------------------- schemas

_PROJECT_ARG = {
    "type": "string",
    "description": "Name of a registered project (see `fitt project list`).",
}

_FEATURE_ARG = {
    "type": "string",
    "description": (
        "Feature/spec folder name under `.kiro/specs/` in the project (e.g. 'phase4-tools')."
    ),
}

_SCHEMA_NONE: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_SCHEMA_PROJECT_ONLY: dict[str, Any] = {
    "type": "object",
    "properties": {"project": _PROJECT_ARG},
    "required": ["project"],
    "additionalProperties": False,
}

_SCHEMA_PROJECT_FEATURE: dict[str, Any] = {
    "type": "object",
    "properties": {"project": _PROJECT_ARG, "feature": _FEATURE_ARG},
    "required": ["project", "feature"],
    "additionalProperties": False,
}

_SCHEMA_MARK_TASK: dict[str, Any] = {
    "type": "object",
    "properties": {
        "project": _PROJECT_ARG,
        "feature": _FEATURE_ARG,
        "task_id": {
            "type": "string",
            "description": ("Task identifier as it appears in tasks.md (e.g. '4a', '10c')."),
        },
    },
    "required": ["project", "feature", "task_id"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- caps + timeouts

_SPEC_READ_MAX_BYTES = 200_000
"""Cap on concatenated spec output. Specs in this repo run ~1000
lines total; 200 KB gives headroom without blowing out the LLM
context."""

_SPEC_LIST_TIMEOUT = 30
_SPEC_READ_TIMEOUT = 30
_SPEC_TASKS_TIMEOUT = 30


# --------------------------------------------------------------- helpers


def _resolve_project_for_tool(
    args: dict[str, Any], ctx: ToolContext
) -> tuple[Any, Any] | ToolResult:
    """Return (project, backend) or a ToolResult error.

    Hub-local and ssh projects are both valid — the backend takes
    care of the wrap. Same pattern as ``fileops.py`` so each tool
    module owns its own arg parsing and failure shapes.
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


def _validate_feature(name: str) -> str | None:
    """Return ``name`` if it's a safe feature identifier, else None.

    Feature names become path components in ``.kiro/specs/<name>``,
    so we reject any character that would let a caller traverse
    (``/``, ``\\``, ``..``) or hide files (leading ``.``).
    """
    if not name:
        return None
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        return None
    return name


def _join_remote_path(root: str, *parts: str) -> str:
    """Join path components with forward slashes.

    We keep this POSIX-only because the backend always invokes a
    POSIX-shell-style command (``cd && <cmd>``), and Git Bash on
    Windows satellites serves ``/c/...`` paths the same way.
    """
    head = root.rstrip("/")
    tail = "/".join(p.strip("/") for p in parts if p)
    return f"{head}/{tail}" if tail else head


# --------------------------------------------------------------- spec_list


async def _tool_spec_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """List feature folders under ``.kiro/specs/``."""
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    specs_dir = _join_remote_path(project.path, ".kiro", "specs")
    # `test -d` returns 0 iff the directory exists; then `ls -1`
    # gives one entry per line. When the directory is missing we
    # return an empty list rather than an error — a project that
    # hasn't written any specs yet is a valid state.
    cmd = [
        "sh",
        "-c",
        (
            f"if [ -d {shlex.quote(specs_dir)} ]; then "
            f"  ls -1 {shlex.quote(specs_dir)} 2>/dev/null; "
            f"fi"
        ),
    ]
    result = await backend.run_shell(project, cmd, timeout_secs=_SPEC_LIST_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"spec_list probe exited {result.exit}").strip())
    features = sorted(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and not line.strip().startswith(".")
    )
    return ToolResult.ok(json.dumps(features, indent=2))


# --------------------------------------------------------------- spec_read


async def _tool_spec_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Return the three spec files concatenated as labelled markdown."""
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    feature_raw = args.get("feature")
    if not isinstance(feature_raw, str):
        return ToolResult.error("Missing required argument: feature")
    feature = _validate_feature(feature_raw)
    if feature is None:
        return ToolResult.error(f"Invalid feature name: {feature_raw!r}")

    spec_dir = _join_remote_path(project.path, ".kiro", "specs", feature)
    out: list[str] = []
    total = 0
    for name in ("requirements.md", "design.md", "tasks.md"):
        file_path = _join_remote_path(spec_dir, name)
        cmd = [
            "sh",
            "-c",
            (
                f"if [ -f {shlex.quote(file_path)} ]; then "
                f"  cat -- {shlex.quote(file_path)}; "
                f"else "
                f"  echo __FITT_MISSING__; "
                f"fi"
            ),
        ]
        result = await backend.run_shell(project, cmd, timeout_secs=_SPEC_READ_TIMEOUT)
        if result.timed_out:
            return ToolResult.error(result.stderr)
        if result.exit != 0:
            out.append(f"## {name}\n\n_(read error: {(result.stderr or 'unknown').strip()})_\n")
            continue
        content = result.stdout
        if content.strip() == "__FITT_MISSING__":
            out.append(f"## {name}\n\n_(missing)_\n")
            continue
        total += len(content)
        if total > _SPEC_READ_MAX_BYTES:
            out.append(
                f"## {name}\n\n_(spec exceeded {_SPEC_READ_MAX_BYTES} bytes; {name} omitted)_\n"
            )
            continue
        out.append(f"## {name}\n\n{content}\n")
    joined = "\n".join(out)
    # Surface a clear "spec not found" error rather than three
    # "_(missing)_" blocks when the whole directory is absent.
    if all("_(missing)_" in chunk for chunk in out):
        return ToolResult.error(f"Spec feature not found: {feature} (looked in {spec_dir})")
    return ToolResult.ok(joined)


# --------------------------------------------------------------- spec_next_task


# Matches lines like "- [ ] 4a. Description..." or "- [x] 10c. ..."
# Capture groups: (1) status char, (2) task id, (3) description-head.
_TASK_LINE = re.compile(
    r"^\s*-\s*\[(?P<status>[ xX])\]\s+(?P<id>\w+)\.\s+(?P<text>.*)$",
    re.MULTILINE,
)


async def _tool_spec_next_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Return the first unchecked task in a feature's tasks.md."""
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    feature_raw = args.get("feature")
    if not isinstance(feature_raw, str):
        return ToolResult.error("Missing required argument: feature")
    feature = _validate_feature(feature_raw)
    if feature is None:
        return ToolResult.error(f"Invalid feature name: {feature_raw!r}")

    tasks_path = _join_remote_path(project.path, ".kiro", "specs", feature, "tasks.md")
    cmd = [
        "sh",
        "-c",
        (
            f"if [ -f {shlex.quote(tasks_path)} ]; then "
            f"  cat -- {shlex.quote(tasks_path)}; "
            f"else "
            f"  echo __FITT_MISSING__; "
            f"fi"
        ),
    ]
    result = await backend.run_shell(project, cmd, timeout_secs=_SPEC_TASKS_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(result.stderr)
    if result.exit != 0:
        return ToolResult.error((result.stderr or f"spec_next_task exited {result.exit}").strip())
    text = result.stdout
    if text.strip() == "__FITT_MISSING__":
        return ToolResult.error(f"No tasks.md for feature {feature!r} at {tasks_path}")

    for m in _TASK_LINE.finditer(text):
        if m.group("status") == " ":
            return ToolResult.ok(
                json.dumps(
                    {
                        "task_id": m.group("id"),
                        "text": m.group("text").strip(),
                    },
                    indent=2,
                )
            )
    return ToolResult.ok(json.dumps({"task_id": None, "text": None}, indent=2))


# --------------------------------------------------------------- spec_mark_task


async def _tool_spec_mark_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Flip a single ``- [ ]`` task to ``- [x]`` in tasks.md.

    The tricky part is doing an atomic write remotely. We read the
    file, mutate locally, then push the modified text back via
    ``sh -c 'cat > path.tmp && mv path.tmp path'``. The mutated
    content is piped on stdin through an ``extra_env`` channel is
    not applicable; we embed it as a heredoc via ``printf %s``
    after base64-encoding to keep the bytes intact across shells.
    """
    resolved = _resolve_project_for_tool(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    feature_raw = args.get("feature")
    task_id = args.get("task_id")
    if not isinstance(feature_raw, str):
        return ToolResult.error("Missing required argument: feature")
    if not isinstance(task_id, str) or not task_id:
        return ToolResult.error("Missing required argument: task_id")
    feature = _validate_feature(feature_raw)
    if feature is None:
        return ToolResult.error(f"Invalid feature name: {feature_raw!r}")

    tasks_path = _join_remote_path(project.path, ".kiro", "specs", feature, "tasks.md")

    # Step 1: read the current tasks.md
    read_cmd = [
        "sh",
        "-c",
        (
            f"if [ -f {shlex.quote(tasks_path)} ]; then "
            f"  cat -- {shlex.quote(tasks_path)}; "
            f"else "
            f"  echo __FITT_MISSING__; "
            f"fi"
        ),
    ]
    read_result = await backend.run_shell(project, read_cmd, timeout_secs=_SPEC_TASKS_TIMEOUT)
    if read_result.timed_out:
        return ToolResult.error(read_result.stderr)
    if read_result.exit != 0:
        return ToolResult.error((read_result.stderr or f"read exited {read_result.exit}").strip())
    text = read_result.stdout
    if text.strip() == "__FITT_MISSING__":
        return ToolResult.error(f"No tasks.md for feature {feature!r} at {tasks_path}")

    # Step 2: find + validate + mutate in memory
    pattern = re.compile(
        r"^(?P<prefix>\s*-\s*\[)(?P<status>[ xX])(?P<mid>\]\s+" + re.escape(task_id) + r"\.\s+.*)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return ToolResult.error(f"No task {task_id!r} found in tasks.md")
    if len(matches) > 1:
        return ToolResult.error(
            f"Task id {task_id!r} is ambiguous: appears {len(matches)} times in tasks.md"
        )
    match = matches[0]
    if match.group("status") != " ":
        return ToolResult.ok(f"Task {task_id!r} already marked done; no change.")
    new_text = text[: match.start("status")] + "x" + text[match.end("status") :]

    # Step 3: atomic write via base64 to survive shell quoting.
    # We pipe the bytes through `base64 -d` on the remote and
    # write to a sibling `.tmp` file, then `mv` over the target.
    import base64

    payload = base64.b64encode(new_text.encode("utf-8")).decode("ascii")
    tmp_path = tasks_path + ".tmp"
    write_cmd = [
        "sh",
        "-c",
        (
            f"printf %s {shlex.quote(payload)} "
            f"| base64 -d > {shlex.quote(tmp_path)} "
            f"&& mv {shlex.quote(tmp_path)} {shlex.quote(tasks_path)}"
        ),
    ]
    write_result = await backend.run_shell(project, write_cmd, timeout_secs=_SPEC_TASKS_TIMEOUT)
    if write_result.timed_out:
        return ToolResult.error(write_result.stderr)
    if write_result.exit != 0:
        return ToolResult.error(
            (write_result.stderr or f"write exited {write_result.exit}").strip()
        )
    return ToolResult.ok(f"Marked task {task_id!r} done in tasks.md")


# --------------------------------------------------------------- builder


def build_inline_tools(registry_ref: Any) -> list[Tool]:
    """Return the set of inline tools that share the registry.

    ``registry_ref`` is the :class:`ToolRegistry` instance; we close
    over it so ``list_capabilities`` can describe the whole set
    (itself included).
    """

    async def _list_capabilities(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        entries = registry_ref.describe_all()
        return ToolResult.ok(json.dumps(entries, indent=2, sort_keys=True))

    return [
        Tool(
            name="list_capabilities",
            description=(
                "Return the list of tools FITT can call, with descriptions and approval buckets."
            ),
            schema=_SCHEMA_NONE,
            callable=_list_capabilities,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
            kind="inline",
        ),
        Tool(
            name="spec_list",
            description=("List spec feature folders under a project's `.kiro/specs/` directory."),
            schema=_SCHEMA_PROJECT_ONLY,
            callable=_tool_spec_list,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="spec_read",
            description=(
                "Read the requirements.md, design.md, and tasks.md files of a spec feature."
            ),
            schema=_SCHEMA_PROJECT_FEATURE,
            callable=_tool_spec_read,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="spec_next_task",
            description=(
                "Return the first unchecked `- [ ]` task id and "
                "description from a feature's tasks.md."
            ),
            schema=_SCHEMA_PROJECT_FEATURE,
            callable=_tool_spec_next_task,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="spec_mark_task",
            description=(
                "Mark a single task complete by flipping its "
                "`- [ ]` checkbox to `- [x]` in tasks.md."
            ),
            schema=_SCHEMA_MARK_TASK,
            callable=_tool_spec_mark_task,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
    ]

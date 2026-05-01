"""Inline tool implementations.

"Inline" means the tool runs inside the gateway process (or via
the SSH backend once Task 5 lands; for now, hub-local only).
Every tool in this module:

* has a callable matching ``ToolCallable`` (async, takes
  ``(args, context) -> ToolResult``);
* is wrapped in a :class:`Tool` entry with its schema and default
  bucket;
* is exposed via :func:`build_inline_tools` so the gateway startup
  can register them in one place.

Task 4 (this file's initial landing) ships the read-only tools:

- ``list_capabilities``: summarise the registry for the model.
- ``spec_list``: list spec features available in a project.
- ``spec_read``: return the requirements/design/tasks files of a
  feature.
- ``spec_next_task``: return the first unchecked task id + text.
- ``spec_mark_task``: flip a ``- [ ]`` to ``- [x]`` (this one's a
  write, but scoped enough to sit in the read-side file for now).

All tools read the project path from :class:`ProjectRegistry`
lookups via ``ToolContext.projects.get(name)``. They refuse to
touch projects whose ``ssh_host`` is non-empty because the SSH
backend (Task 5) is where that branch lives. Raising a clean
``ToolResult.error`` beats a silent hub-local read against a path
that doesn't exist on the hub.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
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


# --------------------------------------------------------------- helpers


def _resolve_project_root(args: dict[str, Any], ctx: ToolContext) -> Path | ToolResult:
    """Resolve ``args['project']`` to a hub-local absolute path.

    Returns a ToolResult error when the project is unknown or lives
    on a remote host (SSH backend lands in Task 5). Returns the
    resolved Path otherwise.
    """
    project_name = args.get("project")
    if not isinstance(project_name, str) or not project_name:
        return ToolResult.error("Missing required argument: project")
    try:
        project = ctx.projects.get(project_name)
    except Exception as exc:  # UnknownProject
        return ToolResult.error(f"Unknown project: {project_name} ({exc})")
    if project.ssh_host:
        return ToolResult.error(
            f"Project {project_name!r} lives on ssh_host "
            f"{project.ssh_host!r}; SSH backend not yet available."
        )
    root = Path(project.path)
    if not root.exists():
        return ToolResult.error(f"Project {project_name!r} path does not exist: {root}")
    return root


def _spec_dir(root: Path, feature: str) -> Path:
    """Return the expected spec directory for a feature."""
    # Keep it paranoid: no path-traversal via feature names.
    if "/" in feature or "\\" in feature or feature.startswith(".") or ".." in feature:
        raise ValueError(f"Invalid feature name: {feature!r}")
    return root / ".kiro" / "specs" / feature


# --------------------------------------------------------------- list_capabilities
#
# ``list_capabilities`` is constructed inside ``build_inline_tools``
# because its callable must close over the registry. No module-level
# function is needed.


# --------------------------------------------------------------- spec_list


async def _tool_spec_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """List spec feature folders under ``.kiro/specs/``."""
    resolved = _resolve_project_root(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    specs_dir = resolved / ".kiro" / "specs"
    if not specs_dir.exists():
        return ToolResult.ok(json.dumps([], indent=2))
    features = sorted(
        p.name for p in specs_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    return ToolResult.ok(json.dumps(features, indent=2))


# --------------------------------------------------------------- spec_read


# Maximum combined size of requirements + design + tasks before we
# truncate. Specs in this repo run ~1000 lines total; 200 KB gives
# generous headroom. Beyond that the LLM context is the bottleneck
# anyway.
_SPEC_READ_MAX_BYTES = 200_000


async def _tool_spec_read(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Return the three spec files concatenated as a labelled string."""
    resolved = _resolve_project_root(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    feature = args.get("feature")
    if not isinstance(feature, str) or not feature:
        return ToolResult.error("Missing required argument: feature")
    try:
        spec_dir = _spec_dir(resolved, feature)
    except ValueError as exc:
        return ToolResult.error(str(exc))
    if not spec_dir.exists():
        return ToolResult.error(f"Spec feature not found: {feature} (looked in {spec_dir})")

    out: list[str] = []
    total = 0
    for name in ("requirements.md", "design.md", "tasks.md"):
        p = spec_dir / name
        if not p.exists():
            out.append(f"## {name}\n\n_(missing)_\n")
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as exc:
            out.append(f"## {name}\n\n_(read error: {exc})_\n")
            continue
        total += len(content)
        if total > _SPEC_READ_MAX_BYTES:
            out.append(
                f"## {name}\n\n_(spec exceeded {_SPEC_READ_MAX_BYTES} bytes; {name} omitted)_\n"
            )
            continue
        out.append(f"## {name}\n\n{content}\n")
    return ToolResult.ok("\n".join(out))


# --------------------------------------------------------------- spec_next_task


# Matches lines like "- [ ] 4a. Description..." or "- [x] 10c. ..."
# Capture groups: (1) status char, (2) task id, (3) description-head.
_TASK_LINE = re.compile(
    r"^\s*-\s*\[(?P<status>[ xX])\]\s+(?P<id>\w+)\.\s+(?P<text>.*)$",
    re.MULTILINE,
)


async def _tool_spec_next_task(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Return the first unchecked task in a feature's tasks.md."""
    resolved = _resolve_project_root(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    feature = args.get("feature")
    if not isinstance(feature, str) or not feature:
        return ToolResult.error("Missing required argument: feature")
    try:
        spec_dir = _spec_dir(resolved, feature)
    except ValueError as exc:
        return ToolResult.error(str(exc))
    tasks_path = spec_dir / "tasks.md"
    if not tasks_path.exists():
        return ToolResult.error(f"No tasks.md for feature {feature!r} at {tasks_path}")
    try:
        text = tasks_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ToolResult.error(f"Could not read tasks.md: {exc}")

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

    Refuses if the task id doesn't appear, or appears more than
    once (ambiguous). Writes atomically via a temp file next to
    tasks.md, then renames.
    """
    resolved = _resolve_project_root(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    feature = args.get("feature")
    task_id = args.get("task_id")
    if not isinstance(feature, str) or not feature:
        return ToolResult.error("Missing required argument: feature")
    if not isinstance(task_id, str) or not task_id:
        return ToolResult.error("Missing required argument: task_id")
    try:
        spec_dir = _spec_dir(resolved, feature)
    except ValueError as exc:
        return ToolResult.error(str(exc))
    tasks_path = spec_dir / "tasks.md"
    if not tasks_path.exists():
        return ToolResult.error(f"No tasks.md for feature {feature!r} at {tasks_path}")
    text = tasks_path.read_text(encoding="utf-8")

    # Build a matcher for this specific task id.
    pattern = re.compile(
        r"^(?P<prefix>\s*-\s*\[)(?P<status>[ xX])(?P<mid>\]\s+" + re.escape(task_id) + r"\.\s+.*)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return ToolResult.error(f"No task {task_id!r} found in {tasks_path.name}")
    if len(matches) > 1:
        return ToolResult.error(
            f"Task id {task_id!r} is ambiguous: appears {len(matches)} times in {tasks_path.name}"
        )
    match = matches[0]
    if match.group("status") != " ":
        return ToolResult.ok(f"Task {task_id!r} already marked done; no change.")

    new_text = text[: match.start("status")] + "x" + text[match.end("status") :]
    # Atomic write: temp in same dir, rename into place.
    tmp = tasks_path.with_suffix(tasks_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(tasks_path)
    return ToolResult.ok(f"Marked task {task_id!r} done in {tasks_path.name}")


# --------------------------------------------------------------- builder


def build_inline_tools(registry_ref: Any) -> list[Tool]:
    """Return the Phase-4-task-4 set of inline tools.

    ``registry_ref`` is the :class:`ToolRegistry` instance the tools
    will close over for ``list_capabilities``. We pass it explicitly
    rather than smuggling it through ``ctx`` so the dependency is
    visible.
    """

    # Capture the registry in a closure so list_capabilities can
    # describe itself (and every other tool) without a context
    # plumbing dance.
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
            # Writes to tasks.md, but scoped to a single checkbox
            # flip — treat it as auto on the hub; per-client policy
            # can override.
            default_bucket=ApprovalBucket.AUTO,
            requires_project=True,
            kind="inline",
        ),
    ]

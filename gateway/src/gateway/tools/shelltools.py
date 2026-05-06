"""Shell-adjacent tools that run something beyond a narrow git/fs
operation.

Two tools live here:

* ``run_tests(project)`` — runs ``project.test_command`` through
  the :class:`~gateway.tools.backend.ExecutionBackend`. A thin
  wrapper by design: the project registry already carries the
  exact command to run, so the tool's only job is to dispatch it
  and surface the result with a large-enough output cap to hold a
  real test report. Default bucket ``ask`` because tests can
  burn CPU, hit networks, and write to the filesystem via fixture
  teardown.

* ``http_get(url)`` — fetches an HTTP(S) URL from the gateway
  process and returns the body as a string. Not project-scoped
  (URLs are global). Default bucket ``auto`` because fetching a
  public doc page is low-risk; operators can tighten via
  ``tools.http_get.deny_hosts`` in ``config.yaml`` for specific
  internal services they want to keep off-limits.

Neither tool opens shell-injection surface: ``run_tests`` splits
the configured command via ``shlex.split`` and hands the argv to
``ExecutionBackend.run_shell`` (which uses ``create_subprocess_exec``
locally, or ``shlex.join``-quoted over SSH). ``http_get`` never
invokes a shell at all — it's an in-process httpx request.

Phase 4 Task 11.
"""

from __future__ import annotations

import fnmatch
import logging
import shlex
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from ..projects import Project

_log = logging.getLogger(__name__)

# --------------------------------------------------------------- caps / timeouts

_RUN_TESTS_CAP_BYTES = 128_000
"""Test output can be chatty. 128 KB covers pytest/unittest for
anything-but-the-largest suites and still fits comfortably in a
model's context window. Tests that legitimately print more than
this are a sign the test suite needs focusing, which is an
observation the model can surface."""

_HTTP_GET_CAP_BYTES = 200_000
"""Generous enough for a typical doc page; small enough that a
misbehaving endpoint (infinite chunked stream, giant response)
can't exhaust context or memory. Binary responses are best
served by a different tool (not v0)."""

_RUN_TESTS_TIMEOUT = 1800
"""Thirty minutes. Longer than any test suite should realistically
take, but shorter than 'forever'. If your suite takes longer than
this, run subsets via test_command + fixture annotations."""

_HTTP_GET_TIMEOUT = 30
"""Per-request timeout. A doc server that's this slow is broken;
we'd rather fail fast than sit on a dead socket."""


# --------------------------------------------------------------- schemas

_PROJECT_ARG = {
    "type": "string",
    "description": "Name of a registered project (see `fitt project list`).",
}

_SCHEMA_RUN_TESTS: dict[str, Any] = {
    "type": "object",
    "properties": {"project": _PROJECT_ARG},
    "required": ["project"],
    "additionalProperties": False,
}

_SCHEMA_HTTP_GET: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": (
                "Absolute URL to GET. http:// and https:// both "
                "accepted; the gateway's deny_hosts list in "
                "config.yaml can block specific hosts."
            ),
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- helpers


def _truncate(out: str, cap: int, label: str) -> str:
    if len(out) <= cap:
        return out
    return out[:cap] + (
        f"\n\n... ({len(out) - cap} more bytes truncated; narrow your {label} to see the rest)"
    )


def _resolve_project(args: dict[str, Any], ctx: ToolContext) -> tuple[Project, Any] | ToolResult:
    """Shared helper for project-scoped tools in this module.

    Returns ``(project, backend)`` on success, or a ``ToolResult``
    error for the caller to short-circuit. Mirrors the helpers in
    ``fileops.py`` / ``gitops.py`` rather than sharing one — a
    future decoupling of any of the three is cheaper this way."""
    project_name = args.get("project")
    if not isinstance(project_name, str) or not project_name:
        return ToolResult.error("Missing required argument: project")
    try:
        project = ctx.projects.get(project_name)
    except Exception as exc:
        return ToolResult.error(f"Unknown project: {project_name} ({exc})")
    if ctx.backend is None:
        return ToolResult.error(
            "Internal error: no execution backend is wired onto the "
            "tool context. This is a gateway bug."
        )
    return project, ctx.backend


# --------------------------------------------------------------- run_tests


async def _tool_run_tests(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Run ``project.test_command`` on the project's execution host."""
    resolved = _resolve_project(args, ctx)
    if isinstance(resolved, ToolResult):
        return resolved
    project, backend = resolved

    if not project.test_command:
        return ToolResult.error(
            f"Project {project.name!r} has no test_command configured. "
            f"Edit projects.yaml (or `fitt project add` with --test-command)."
        )

    # Split the configured command into argv. This is deliberate:
    # we avoid the shell wrapping that the SSH path does anyway,
    # so the command runs with the same semantics hub-local and
    # over SSH. If a user wants shell features in their
    # test_command (pipes, &&), they can set it to
    # ``sh -c "cd subdir && pytest"`` themselves — explicit.
    try:
        cmd = shlex.split(project.test_command)
    except ValueError as e:
        return ToolResult.error(
            f"test_command for {project.name!r} failed to parse: {e}. "
            "Check quoting in projects.yaml."
        )
    if not cmd:
        return ToolResult.error(f"Project {project.name!r} has an empty test_command.")

    result = await backend.run_shell(project, cmd, timeout_secs=_RUN_TESTS_TIMEOUT)
    if result.timed_out:
        return ToolResult.error(
            f"test run timed out after {_RUN_TESTS_TIMEOUT}s (command: {project.test_command!r})"
        )

    # Tests send useful info to BOTH stdout (pytest progress,
    # assertion diffs) and stderr (warnings, import errors). Model
    # wants to see both to reason about failures, so we merge.
    merged = result.stdout
    if result.stderr:
        merged += "\n--- stderr ---\n" + result.stderr

    summary_prefix = f"exit={result.exit} ({'passed' if result.exit == 0 else 'failed'})\n\n"
    payload = summary_prefix + merged
    capped = _truncate(payload, _RUN_TESTS_CAP_BYTES, "test command or suite")
    if result.exit == 0:
        return ToolResult.ok(capped)
    # Non-zero: still return the output, but as an error so the
    # model's tool-result handling knows to react (re-read, retry,
    # ask for guidance).
    return ToolResult.error(capped)


# --------------------------------------------------------------- http_get


def _host_of(url: str) -> str | None:
    """Extract the lowercase hostname from a URL, or None on parse failure."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    return (parsed.hostname or "").lower() or None


def _is_denied(host: str, deny_patterns: list[str]) -> bool:
    """Return True if host matches any fnmatch pattern in ``deny_patterns``."""
    for pattern in deny_patterns:
        if fnmatch.fnmatchcase(host, pattern.lower()):
            return True
    return False


async def _tool_http_get(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Fetch an http(s) URL and return its body as text."""
    url = args.get("url")
    if not isinstance(url, str) or not url:
        return ToolResult.error("Missing required argument: url")

    host = _host_of(url)
    if host is None:
        return ToolResult.error(
            f"URL rejected (must be an absolute http:// or https:// URL): {url!r}"
        )

    # Read deny_hosts from per-tool extras. Absent or empty list
    # means "no host is denied" — the default shipping config.
    deny_hosts: list[str] = []
    if ctx.policy is not None:
        extras = ctx.policy.per_tool_extras.get("http_get", {})
        raw = extras.get("deny_hosts")
        if isinstance(raw, list):
            deny_hosts = [str(p) for p in raw if isinstance(p, str)]

    if deny_hosts and _is_denied(host, deny_hosts):
        return ToolResult.error(
            f"host {host!r} is on the http_get deny_hosts list. Edit "
            f"tools.http_get.deny_hosts in config.yaml to change."
        )

    try:
        async with httpx.AsyncClient(timeout=_HTTP_GET_TIMEOUT) as client:
            response = await client.get(url, follow_redirects=True)
    except httpx.TimeoutException:
        return ToolResult.error(f"http_get timed out after {_HTTP_GET_TIMEOUT}s")
    except httpx.HTTPError as e:
        return ToolResult.error(f"http_get transport error: {e}")

    if response.status_code >= 400:
        return ToolResult.error(f"HTTP {response.status_code} from {host}: {response.text[:500]}")
    body = response.text
    summary = f"HTTP {response.status_code} {host} ({len(body)} chars)\n\n"
    return ToolResult.ok(summary + _truncate(body, _HTTP_GET_CAP_BYTES, "URL"))


# --------------------------------------------------------------- builder


def build_shell_tools() -> list[Tool]:
    """Return the Phase-4-task-11 shell-adjacent tools."""
    return [
        Tool(
            name="run_tests",
            description=(
                "Run the configured test suite for a project. Uses "
                "the project's test_command (set via `fitt project "
                "add --test-command`). Returns stdout + stderr with "
                "exit status; failures are surfaced as tool errors."
            ),
            schema=_SCHEMA_RUN_TESTS,
            callable=_tool_run_tests,
            default_bucket=ApprovalBucket.ASK,
            requires_project=True,
            kind="inline",
        ),
        Tool(
            name="http_get",
            description=(
                "Fetch an HTTP(S) URL from the gateway. Follows "
                "redirects; caps response body; rejects hosts "
                "listed in config.yaml's tools.http_get.deny_hosts."
            ),
            schema=_SCHEMA_HTTP_GET,
            callable=_tool_http_get,
            default_bucket=ApprovalBucket.AUTO,
            requires_project=False,
            kind="inline",
        ),
    ]

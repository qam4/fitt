"""Shared types for the tools subsystem.

These dataclasses are what every tool-related module speaks in:

* ``Tool`` — the registry entry: name, schema, implementation,
  default approval bucket, metadata.
* ``ToolResult`` — what a tool returns to the caller. Success is a
  string payload; failure is a string error. Kept stringly-typed
  because the downstream consumer is always a chat completion
  tool-result message, which is itself a string.
* ``ApprovalBucket`` — where a tool call sits on the policy
  spectrum. Resolved per-call by the registry.
* ``ApprovalDecision`` — the output of the approval pipeline that
  drives dispatch: do we run, skip, or block?
* ``ToolContext`` — per-request context the tool callable needs
  (who is calling, which session, the registries it can read).

Everything here is importable without pulling in heavier modules
(subprocess backend, audit log, telegram), so policy and registry
code can reason about tools without circular deps.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    # Avoid the import cycle: projects.py already imports from config
    # and we don't want tools/* to pull the project registry eagerly.
    from ..projects import ProjectRegistry
    from .registry import ToolPolicy


# --------------------------------------------------------------- buckets


class ApprovalBucket(StrEnum):
    """Where a tool call sits on the approval spectrum.

    Values are strings so policy YAML can use the lowercase names
    directly (``default: auto``) without bespoke serialisation.
    """

    AUTO = "auto"
    """Run without asking. Reserved for reads and for the small set
    of writes a client has explicitly signalled it's watching
    (IDE agent-mode edits)."""

    ASK = "ask"
    """Pause and ask for approval before running. The approval
    middleware routes the prompt to the right UI (Telegram, IDE
    native UI, ...)."""

    TRUST_SESSION = "trust_session"
    """Same as ask, but remember 'yes' for the rest of the session
    so we don't prompt twice for the same tool. Reset on session
    switch."""

    YOLO = "yolo"
    """Auto-approve everything until a timer expires. Per-client
    timer (e.g. 30 min for telegram, 6 h for ide)."""

    BLOCK = "block"
    """Never run. Useful for locking a tool to a subset of clients
    (e.g. Open WebUI read-only by blocking all write_* tools)."""


# --------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returns.

    ``ok`` is a string payload that becomes the content of the
    tool-result message sent back to the LLM. ``error`` is a
    human-readable failure string (also returned to the LLM so the
    model can reason about the failure, retry, or give up).
    """

    payload: str
    is_error: bool = False

    @classmethod
    def ok(cls, payload: str) -> ToolResult:
        return cls(payload=payload, is_error=False)

    @classmethod
    def error(cls, message: str) -> ToolResult:
        return cls(payload=message, is_error=True)


# --------------------------------------------------------------- tool entry

# Defined as a type alias rather than an ABC because the registry
# only ever *calls* tools; it doesn't need subclass hooks.
ToolCallable = Callable[["dict[str, Any]", "ToolContext"], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class Tool:
    """One entry in the tool registry."""

    name: str
    """Stable identifier used by the model and by policy lookups.
    For inline tools: ``read_file``, ``write_file``. For MCP
    tools: ``mcp.<server>.<tool>`` (the prefix is how we know
    which subprocess to route the call to)."""

    description: str
    """One-line human-readable summary, shown to the model in the
    capability block and to the user in approval prompts."""

    schema: dict[str, Any]
    """JSON schema for arguments, in the shape OpenAI / Anthropic
    tool-calling APIs expect. Validated against incoming args."""

    callable: ToolCallable
    """The async implementation. For inline tools this is a Python
    function; for MCP tools it's a thin wrapper that forwards to
    the subprocess supervisor."""

    default_bucket: ApprovalBucket
    """The bucket we'd assign in the absence of any per-tool or
    per-client override. Overridable at registration time via
    ``ToolPolicy``."""

    requires_project: bool = False
    """``True`` when ``args`` must include a ``project`` field
    (pretty much everything touching files or running commands).
    Validated before dispatch."""

    kind: Literal["inline", "mcp"] = "inline"
    """Where the tool's implementation lives. Used for audit log
    discrimination and for MCP lifecycle management (restart a
    server -> deregister its tools)."""

    shell_command_for: Callable[[dict[str, Any]], str | None] | None = None
    """Optional hook: given the tool's arguments, return the
    shell command string that would be dispatched (for deny-list
    matching), or ``None`` if the tool doesn't feed
    model-controlled strings into a shell context.

    Today, **no inline tool needs this** — ``run_tests`` uses the
    operator-configured ``project.test_command`` (not model
    input), ``git_commit`` passes the message as a single argv
    element, ``write_file`` and ``edit_file`` isolate content via
    stdin. The hook exists for the future ``project_shell`` tool
    (F2 follow-up) and for MCP tools that wrap arbitrary shell
    commands.

    When the hook returns a non-None string, the approval
    middleware runs the deny list against it before any bucket
    resolution; a match hard-blocks regardless of client trust."""

    def to_openai_schema(self) -> dict[str, Any]:
        """Render as an entry in an OpenAI ``tools=[]`` request array."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema,
            },
        }


# --------------------------------------------------------------- context


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-request context passed to every tool callable.

    Bundled so tools don't each grow a half-dozen parameters, and
    so we can add new fields (audit, capability-gap logger, ...)
    without touching every tool signature.
    """

    client: str
    """Which client is calling: ``ide``, ``telegram``, ``webui``,
    ``cli``. Resolved from the Bearer token by the auth
    middleware."""

    session_key: str
    """Session identifier (``main`` by default). Used for
    per-session approval trust and for scoping audit entries."""

    projects: ProjectRegistry
    """The live project registry. Tools call ``projects.get(name)``
    to resolve a ``project`` argument into its full record
    (path, ssh_host, test_command, ...)."""

    backend: Any = None
    """The :class:`~gateway.tools.backend.ExecutionBackend` that
    runs shell commands on the project's execution host. Typed
    as Any to avoid an import cycle (backend imports projects,
    projects imports nothing in tools). Tools that need it
    should cast/type-ignore at the call site. Optional because
    the Task 4 read-only spec tools don't need it."""

    policy: ToolPolicy | None = None
    """The parsed ``tools:`` section of config.yaml. Tools that
    have per-tool config fields (``deny_hosts`` on ``http_get``,
    for example) read them via
    ``ctx.policy.per_tool_extras.get(tool_name, {})``. Optional
    so tests can supply a minimal context without building a
    policy."""

    audit: Any = None
    """The :class:`~gateway.audit.AuditLog` the chat loop uses to
    record every tool call. Typed ``Any`` to avoid an import
    cycle between ``_types`` and ``audit`` (which in turn imports
    nothing from tools; keeping ``_types`` minimal keeps both
    sides importable). Optional for tests that don't exercise the
    audit path."""

    cron: Any = None
    """The :class:`~gateway.cron.CronService` the cron tools
    (``cron_add``, ``cron_list``, ...) operate on. Typed ``Any``
    for the same import-cycle reason as ``audit``. ``None`` in
    contexts where no cron tools are registered — the cron tools
    themselves fail with a readable error when it's missing."""

    events: Any = None
    """The :class:`~gateway.events.EventLog`. Written to by cron
    firings, ``send_message``, and the detached-delivery worker.
    ``None`` when there's no event log wired (tests); tools that
    need it fail gracefully."""

    local_shell: Any = None
    """The :class:`~gateway.tools.local_shell.ShellInterpreter`
    resolved at gateway boot. ``project_shell`` uses it to build
    the ``bash -lc <command>`` argv for local (non-SSH)
    projects. ``None`` in tests that don't exercise
    ``project_shell`` — the tool fails with a readable error
    when it's missing."""

    lessons: Any = None
    """The :class:`~gateway.lessons.LessonsStore` the
    ``learn_*`` tools mutate. Typed ``Any`` to dodge the
    import cycle ``_types`` would otherwise create with
    ``lessons``. ``None`` in tests that don't exercise the
    learn tools; those tools fail with a readable error when
    it's missing."""

    turns: Any = None
    """The :class:`~gateway.turns.TurnLog` that records
    per-turn events (Phase 4.8). ``None`` disables turn-event
    emission — the emission helpers short-circuit on a
    ``None`` turns argument, so tests that don't exercise
    the event stream don't need to wire it. Typed ``Any``
    for the same import-cycle reason as other fields above."""

    turn_id: str | None = None
    """Stable identifier for the turn this tool invocation
    belongs to (Phase 4.8). Generated once per chat request
    in :mod:`gateway.chat` or per cron firing in
    :mod:`gateway.cron_runner`. Turn events emitted during
    the turn carry this id so renderers can group them.
    ``None`` disables turn-event emission — callers that
    haven't threaded a turn id through can leave the field
    default and the emission helpers do nothing."""


# --------------------------------------------------------------- decisions


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """Output of the approval pipeline.

    The dispatcher reads ``execute`` to decide whether to actually
    run the tool. ``reason`` is a short code that goes in the
    audit log; ``detail`` is a human-readable elaboration shown to
    the model and the user when ``execute=False``.
    """

    execute: bool
    reason: Literal[
        "auto",
        "approved",
        "rejected",
        "timeout",
        "trust_session",
        "yolo",
        "blocked",
        "denied_deny_list",
    ]
    detail: str = ""

    # Convenience constructors so callers can write intent, not
    # plumbing. (Every factory below is what the approval
    # middleware returns in one of the documented branches.)

    @classmethod
    def auto(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=True, reason="auto", detail=detail)

    @classmethod
    def approved(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=True, reason="approved", detail=detail)

    @classmethod
    def rejected(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=False, reason="rejected", detail=detail)

    @classmethod
    def timeout(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=False, reason="timeout", detail=detail)

    @classmethod
    def trust_session(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=True, reason="trust_session", detail=detail)

    @classmethod
    def yolo(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=True, reason="yolo", detail=detail)

    @classmethod
    def blocked(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=False, reason="blocked", detail=detail)

    @classmethod
    def denied_deny_list(cls, detail: str = "") -> ApprovalDecision:
        return cls(execute=False, reason="denied_deny_list", detail=detail)

"""Tool registry + policy resolver.

Two collaborating objects, both intentionally tiny:

* ``ToolPolicy`` — the parsed ``tools:`` section of config.yaml.
  Pure data; tells us how buckets are overridden per-tool, per
  tool+client, and per-wildcard (for MCP).
* ``ToolRegistry`` — the in-memory set of registered tools plus a
  ``resolve_bucket`` method that walks the documented precedence
  chain to decide how a given tool call should be approved.

Tool *implementations* are registered from elsewhere (inline
decorators in Task 4, MCP subprocess probes in Task 14). This
module only cares about names, schemas, default buckets, and the
policy ladder.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..errors import DuplicateTool, UnknownTool
from ._types import ApprovalBucket, Tool

_log = logging.getLogger(__name__)

# --------------------------------------------------------------- defaults


# Client defaults are the last-resort bucket when no per-tool or
# wildcard override matches. Hand-picked to fail closed for the
# least-trusted clients and stay out of the way for the IDE case
# where the user is watching every call natively.
_CLIENT_DEFAULTS: dict[str, ApprovalBucket] = {
    "ide": ApprovalBucket.AUTO,
    "cli": ApprovalBucket.ASK,
    "telegram": ApprovalBucket.ASK,
    "webui": ApprovalBucket.ASK,
    "unknown": ApprovalBucket.ASK,
}


_GLOBAL_FALLBACK = ApprovalBucket.ASK
"""Used only when the client tag is neither a known label nor
``unknown``; shouldn't happen in practice since the auth
middleware normalises the tag, but we fail-closed if it does."""


# --------------------------------------------------------------- policy


class _ToolEntry(BaseModel):
    """One entry under ``tools:`` keyed by tool name (or wildcard).

    The only field we actually consume today is ``default``; future
    fields (``deny_hosts`` for ``http_get``) live here so the YAML
    parses cleanly and the enforcer picks them up later.
    """

    model_config = ConfigDict(extra="allow")

    default: ApprovalBucket | None = None


class ToolPolicyConfig(BaseModel):
    """Schema for the ``tools:`` section of config.yaml.

    Shape:

    .. code-block:: yaml

        tools:
          read_file:        { default: auto }
          write_file:       { default: ask }
          "mcp.slack.*":    { default: ask }
          per_client:
            ide:
              write_file: auto
            webui:
              write_file: block

    ``per_client`` is optional and keyed by client tag.
    """

    model_config = ConfigDict(extra="forbid")

    # Per-tool defaults, keyed by tool name or fnmatch wildcard
    # (for MCP). Anything that isn't ``per_client`` is treated as
    # a tool-name key.
    per_tool: dict[str, _ToolEntry] = Field(default_factory=dict)
    per_client: dict[str, dict[str, ApprovalBucket]] = Field(default_factory=dict)

    # How long the middleware waits for a human to respond to an
    # ``ask`` / ``trust_session`` prompt before timing out the
    # pending approval. Short default (45s) because the
    # gateway's HTTP chat request holds its TCP connection for
    # this long and most HTTP clients (including the Telegram
    # bot) cap at 60-120s. Two-hour-plus workflows belong in
    # Phase 4.5 (event log + proactive push) so the chat turn
    # can return immediately while the tool result is delivered
    # asynchronously. Set in config.yaml with:
    #
    # .. code-block:: yaml
    #
    #     tools:
    #       approval_timeout_secs: 30
    approval_timeout_secs: float | None = None

    # Phase 4.5 Task 5.5. When a chat turn's ask-bucket approval
    # is still pending after this many seconds, the chat handler
    # detaches: returns a placeholder HTTP response immediately
    # and lets the remaining tool-loop iterations finish in the
    # background. The eventual result lands as a
    # ``late_tool_result`` (or ``late_tool_rejected``) event so
    # the user still hears about it, just asynchronously.
    #
    # Default ``None`` means "mirror ``approval_timeout_secs``",
    # which effectively disables the feature — the inner timeout
    # fires at the same moment the outer detach threshold would,
    # so the tool loop continues with an error and the handler
    # returns synchronously. To activate detach, set this below
    # ``approval_timeout_secs``; a typical "live with it" config
    # is ``approval_detach_threshold_secs: 45`` +
    # ``approval_timeout_secs: 7200`` so the HTTP client detaches
    # at 45s but the approval keeps listening for two hours.
    approval_detach_threshold_secs: float | None = None

    # Pydantic 2 handles the custom layout via a before-validator
    # so we don't force users to type an extra ``per_tool:`` key
    # in the YAML.
    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> ToolPolicyConfig:
        """Build from the raw YAML mapping.

        The YAML lets users write ``tools: {read_file: ...,
        per_client: ...}`` where everything except ``per_client``
        and a handful of reserved scalar knobs is a tool entry.
        We split that apart here.
        """
        if not raw:
            return cls()
        per_client = raw.get("per_client", {}) or {}
        approval_timeout_secs = raw.get("approval_timeout_secs")
        approval_detach_threshold_secs = raw.get("approval_detach_threshold_secs")
        reserved = {
            "per_client",
            "approval_timeout_secs",
            "approval_detach_threshold_secs",
        }
        per_tool_raw = {k: v for k, v in raw.items() if k not in reserved}
        return cls(
            per_tool={k: _ToolEntry.model_validate(v or {}) for k, v in per_tool_raw.items()},
            per_client={
                client: {tool: ApprovalBucket(bucket) for tool, bucket in (overrides or {}).items()}
                for client, overrides in per_client.items()
            },
            approval_timeout_secs=approval_timeout_secs,
            approval_detach_threshold_secs=approval_detach_threshold_secs,
        )


@dataclass(slots=True)
class ToolPolicy:
    """Runtime-facing policy object.

    Separated from the Pydantic schema so the registry doesn't
    need to depend on config loading. Build with
    ``ToolPolicy.from_config(cfg)``; tests can build directly.
    """

    per_tool_default: dict[str, ApprovalBucket] = field(default_factory=dict)
    """Exact-name overrides. e.g. ``{"read_file": AUTO}``."""

    per_tool_wildcard: list[tuple[str, ApprovalBucket]] = field(default_factory=list)
    """Wildcard patterns in order of declaration. First match
    wins. e.g. ``[("mcp.slack.*", ASK), ("mcp.jira.search_*", AUTO)]``."""

    per_client: dict[str, dict[str, ApprovalBucket]] = field(default_factory=dict)
    """Nested overrides keyed by client tag, then by tool name.
    e.g. ``{"ide": {"write_file": AUTO}, "webui": {"write_file": BLOCK}}``."""

    per_tool_extras: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Extra config fields declared under a tool entry, excluding
    ``default``. For example ``{"http_get": {"deny_hosts": [...]}}``.
    Tools read their own extras via
    ``ctx.policy.per_tool_extras.get(tool_name, {})``. Exposed as a
    dict so individual tools don't have to share a Pydantic schema
    just to carry a couple of config fields."""

    approval_timeout_secs: float | None = None
    """How long the middleware waits on an ``ask`` /
    ``trust_session`` future before auto-rejecting. ``None`` means
    use the middleware's built-in default (see
    ``gateway.approval._DEFAULT_APPROVAL_TIMEOUT_S``)."""

    approval_detach_threshold_secs: float | None = None
    """Phase 4.5 Task 5.5. When a chat turn's ask-bucket approval
    is still pending after this many seconds, the HTTP handler
    returns a placeholder and hands the remaining tool-loop
    work to a background worker that emits a
    ``late_tool_result`` / ``late_tool_rejected`` event when it
    finishes. ``None`` means "don't detach" (mirror
    ``approval_timeout_secs`` in effect), which is the zero-
    configuration default so operators opt in explicitly when
    they're ready for two-hour workflows.

    Set below ``approval_timeout_secs`` to activate detach: the
    chat handler detaches at this threshold, while the approval
    middleware keeps listening for up to ``approval_timeout_secs``
    in the background."""

    @classmethod
    def from_config(cls, raw_tools: dict[str, Any] | None) -> ToolPolicy:
        """Parse the ``tools:`` block from config.yaml."""
        parsed = ToolPolicyConfig.from_raw(raw_tools)
        per_tool_default: dict[str, ApprovalBucket] = {}
        per_tool_wildcard: list[tuple[str, ApprovalBucket]] = []
        per_tool_extras: dict[str, dict[str, Any]] = {}
        for name, entry in parsed.per_tool.items():
            if entry.default is not None:
                if _is_wildcard(name):
                    per_tool_wildcard.append((name, entry.default))
                else:
                    per_tool_default[name] = entry.default
            # Capture extras regardless of whether ``default`` was
            # set — a tool entry with only ``deny_hosts`` is still
            # valid (keeps the default bucket, tweaks behaviour).
            extras = {
                k: v for k, v in entry.model_dump(exclude_none=False).items() if k != "default"
            }
            if extras:
                per_tool_extras[name] = extras
        return cls(
            per_tool_default=per_tool_default,
            per_tool_wildcard=per_tool_wildcard,
            per_client=dict(parsed.per_client),
            per_tool_extras=per_tool_extras,
            approval_timeout_secs=parsed.approval_timeout_secs,
            approval_detach_threshold_secs=parsed.approval_detach_threshold_secs,
        )


def _is_wildcard(name: str) -> bool:
    """Treat any name containing ``*`` or ``?`` as an fnmatch pattern."""
    return "*" in name or "?" in name


# --------------------------------------------------------------- registry


class ToolRegistry:
    """In-memory set of registered tools with policy-aware bucket resolution.

    Registration is explicit: inline tools call ``register`` at
    import time (or via a decorator helper in Task 4); MCP tools
    register after a successful ``tools/list`` probe.

    Session trust lives here too because it's a property of the
    registry's view of the world (which tool, which session)
    rather than of any single tool.
    """

    def __init__(self, policy: ToolPolicy | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._policy = policy or ToolPolicy()
        # session_key -> set of tool names the user has trust-session-approved
        self._session_trust: dict[str, set[str]] = {}
        # tool_name -> {client -> ApprovalBucket} baked in at
        # registration time by the tool author. Applied after
        # operator config (``tools.per_client``) and before the
        # generic per-client fallback so operators can still
        # override but the default reflects each tool's
        # risk posture. Phase 4.7 introduces this for
        # ``project_shell``: ask on CLI/Telegram/IDE,
        # block on Open WebUI.
        self._per_tool_baked_in: dict[str, dict[str, ApprovalBucket]] = {}

    @property
    def policy(self) -> ToolPolicy:
        """Read-only view of the registry's policy.

        Callers (chat.py, tests) use this to build a ToolContext
        without reaching into the registry's internals. Read-only
        because mutating policy at runtime would be a surprise —
        policy is expected to be parsed once from config.yaml at
        startup."""
        return self._policy

    # ---------------------------------------------- register / lookup

    def register(
        self,
        tool: Tool,
        *,
        per_client_defaults: dict[str, ApprovalBucket] | None = None,
    ) -> None:
        """Add a tool to the registry.

        Raises ``DuplicateTool`` if the name is already taken. The
        caller is expected to ``unregister`` first when replacing
        (MCP reload flow).

        ``per_client_defaults`` is an optional baked-in per-tool
        per-client override map. It applies BEFORE the generic
        ``_CLIENT_DEFAULTS`` fallback but AFTER operator config
        (``tools.per_client`` in config.yaml) — so a tool author
        can set sensible defaults without blocking the operator
        from tightening or loosening them. Used by Phase 4.7's
        ``project_shell`` to default Open WebUI to ``block``.
        """
        if tool.name in self._tools:
            raise DuplicateTool(tool.name)
        self._tools[tool.name] = tool
        if per_client_defaults:
            # Copy so the caller can't mutate what we've stored.
            self._per_tool_baked_in[tool.name] = dict(per_client_defaults)
        _log.info("tool.registered", extra={"tool": tool.name, "kind": tool.kind})

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if the name is unknown.

        Used by the MCP supervisor when a server dies or restarts:
        its tools disappear from the registry and reappear on the
        next successful probe.
        """
        existed = self._tools.pop(name, None)
        if existed is not None:
            self._per_tool_baked_in.pop(name, None)
            _log.info("tool.unregistered", extra={"tool": name})
            # Clear any trust_session grants for the removed tool so
            # a replacement can't inherit stale approval.
            for trusted in self._session_trust.values():
                trusted.discard(name)

    def lookup(self, name: str) -> Tool:
        """Return the tool entry or raise ``UnknownTool``."""
        try:
            return self._tools[name]
        except KeyError as e:
            raise UnknownTool(name, self.list_names()) from e

    def has(self, name: str) -> bool:
        """Non-raising ``lookup``: just returns a bool. Used by the
        chat dispatcher to decide whether a tool call is FITT-owned
        or belongs to the client."""
        return name in self._tools

    def list_names(self) -> list[str]:
        """Sorted list of registered tool names."""
        return sorted(self._tools.keys())

    def list_all(self) -> list[Tool]:
        """All registered tools, sorted by name."""
        return [self._tools[n] for n in self.list_names()]

    def describe_all(self) -> list[dict[str, Any]]:
        """Summaries suitable for the capability block and for the
        ``list_capabilities`` tool."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "bucket": t.default_bucket.value,
                "kind": t.kind,
                "requires_project": t.requires_project,
            }
            for t in self.list_all()
        ]

    # ---------------------------------------------- session trust

    def trust_for_session(self, session_key: str, tool_name: str) -> None:
        """Mark ``tool_name`` as trusted for the rest of ``session_key``.

        Approval middleware calls this after the user responds
        "Trust session" to an ``ask`` prompt. The grant is in-memory
        only: a gateway restart wipes it, which is the intended
        fail-closed behaviour.
        """
        self._session_trust.setdefault(session_key, set()).add(tool_name)

    def is_trusted_for_session(self, session_key: str, tool_name: str) -> bool:
        return tool_name in self._session_trust.get(session_key, set())

    def forget_session_trust(self, session_key: str) -> None:
        """Drop all trust_session grants for a session.

        Called when the user explicitly ends or switches session.
        """
        self._session_trust.pop(session_key, None)

    # ---------------------------------------------- bucket resolution

    def resolve_bucket(
        self,
        tool: Tool,
        client: str,
        session_key: str,
    ) -> ApprovalBucket:
        """Return the effective approval bucket for this call.

        Precedence chain (highest priority first):

        1. Per-tool override for this specific client
           (``per_client[client][tool.name]``).
        2. Per-tool exact-name default (``per_tool[tool.name]``).
        3. Wildcard match against the tool name (first match
           wins; MCP tools key off this).
        4. Baked-in per-tool per-client default registered by
           the tool author (``register(per_client_defaults=...)``).
        5. The tool's own ``default_bucket`` attribute (baked in
           at registration).
        6. Client default from the hard-coded table.
        7. Global fallback (``ASK``).

        ``session_key`` is accepted here for a stable signature
        because the approval middleware will later consult
        per-session trust at this level. The registry itself has
        a dedicated ``is_trusted_for_session`` method, so the
        middleware composes the two explicitly.
        """
        _ = session_key  # reserved for per-session bucket overrides
        # 1. per-client, per-tool
        client_overrides = self._policy.per_client.get(client, {})
        if tool.name in client_overrides:
            return client_overrides[tool.name]

        # 2. per-tool exact name
        if tool.name in self._policy.per_tool_default:
            return self._policy.per_tool_default[tool.name]

        # 3. wildcard match (first-declared wins)
        for pattern, bucket in self._policy.per_tool_wildcard:
            if fnmatch.fnmatchcase(tool.name, pattern):
                return bucket

        # 4. baked-in per-tool per-client defaults (registered
        # by the tool author via ``register(per_client_defaults=...)``).
        # Lets ``project_shell`` default to ``block`` on webui
        # without operator config, while still letting the
        # operator override via ``tools.per_client.webui.project_shell``.
        baked = self._per_tool_baked_in.get(tool.name)
        if baked and client in baked:
            return baked[client]

        # 5. tool's own default
        if tool.default_bucket is not None:
            return tool.default_bucket

        # 6. client default
        if client in _CLIENT_DEFAULTS:
            return _CLIENT_DEFAULTS[client]

        # 7. global fallback
        return _GLOBAL_FALLBACK

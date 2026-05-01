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

    # Pydantic 2 handles the custom layout via a before-validator
    # so we don't force users to type an extra ``per_tool:`` key
    # in the YAML.
    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> ToolPolicyConfig:
        """Build from the raw YAML mapping.

        The YAML lets users write ``tools: {read_file: ...,
        per_client: ...}`` where everything except ``per_client``
        is a tool entry. We split that apart here.
        """
        if not raw:
            return cls()
        per_client = raw.get("per_client", {}) or {}
        per_tool_raw = {k: v for k, v in raw.items() if k != "per_client"}
        return cls(
            per_tool={k: _ToolEntry.model_validate(v or {}) for k, v in per_tool_raw.items()},
            per_client={
                client: {tool: ApprovalBucket(bucket) for tool, bucket in (overrides or {}).items()}
                for client, overrides in per_client.items()
            },
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

    @classmethod
    def from_config(cls, raw_tools: dict[str, Any] | None) -> ToolPolicy:
        """Parse the ``tools:`` block from config.yaml."""
        parsed = ToolPolicyConfig.from_raw(raw_tools)
        per_tool_default: dict[str, ApprovalBucket] = {}
        per_tool_wildcard: list[tuple[str, ApprovalBucket]] = []
        for name, entry in parsed.per_tool.items():
            if entry.default is None:
                # Name appears in YAML but defines no bucket; skip.
                continue
            if _is_wildcard(name):
                per_tool_wildcard.append((name, entry.default))
            else:
                per_tool_default[name] = entry.default
        return cls(
            per_tool_default=per_tool_default,
            per_tool_wildcard=per_tool_wildcard,
            per_client=dict(parsed.per_client),
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

    # ---------------------------------------------- register / lookup

    def register(self, tool: Tool) -> None:
        """Add a tool to the registry.

        Raises ``DuplicateTool`` if the name is already taken. The
        caller is expected to ``unregister`` first when replacing
        (MCP reload flow).
        """
        if tool.name in self._tools:
            raise DuplicateTool(tool.name)
        self._tools[tool.name] = tool
        _log.info("tool.registered", extra={"tool": tool.name, "kind": tool.kind})

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if the name is unknown.

        Used by the MCP supervisor when a server dies or restarts:
        its tools disappear from the registry and reappear on the
        next successful probe.
        """
        existed = self._tools.pop(name, None)
        if existed is not None:
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
        4. The tool's own ``default_bucket`` attribute (baked in
           at registration).
        5. Client default from the hard-coded table.
        6. Global fallback (``ASK``).

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

        # 4. tool's own default
        if tool.default_bucket is not None:
            return tool.default_bucket

        # 5. client default
        if client in _CLIENT_DEFAULTS:
            return _CLIENT_DEFAULTS[client]

        # 6. global fallback
        return _GLOBAL_FALLBACK

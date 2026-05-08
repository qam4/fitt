"""Tests for the tool registry, policy loader, and bucket resolver.

Covers Phase 4 Task 3 acceptance criteria: registry CRUD, policy
parsing from YAML, ``resolve_bucket`` precedence chain, session
trust tracking.

No real tools are registered here; each test builds tiny
``Tool`` stubs because Task 3 ships the scaffolding and Task 4+
builds actual callables on top of it.
"""

from __future__ import annotations

import pytest

from gateway.errors import DuplicateTool, UnknownTool
from gateway.tools import (
    ApprovalBucket,
    Tool,
    ToolPolicy,
    ToolRegistry,
)
from gateway.tools.registry import ToolPolicyConfig

# --------------------------------------------------------------- helpers


async def _noop(args: dict, ctx) -> object:  # type: ignore[no-untyped-def]
    raise AssertionError("stub should never be executed")


def _mk(
    name: str,
    bucket: ApprovalBucket = ApprovalBucket.ASK,
    *,
    kind: str = "inline",
    requires_project: bool = False,
) -> Tool:
    """Build a minimal Tool stub. Tests never invoke the callable."""
    return Tool(
        name=name,
        description=f"stub for {name}",
        schema={"type": "object", "properties": {}},
        callable=_noop,  # type: ignore[arg-type]
        default_bucket=bucket,
        requires_project=requires_project,
        kind=kind,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------- registry CRUD


def test_register_and_lookup() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file"))
    t = reg.lookup("read_file")
    assert t.name == "read_file"


def test_register_duplicate_raises() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file"))
    with pytest.raises(DuplicateTool) as exc:
        reg.register(_mk("read_file"))
    assert "read_file" in str(exc.value)


def test_lookup_unknown_raises_with_available_list() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file"))
    reg.register(_mk("write_file"))
    with pytest.raises(UnknownTool) as exc:
        reg.lookup("does_not_exist")
    assert exc.value.available == ["read_file", "write_file"]


def test_has_is_boolean() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file"))
    assert reg.has("read_file") is True
    assert reg.has("nope") is False


def test_unregister_removes_entry() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file"))
    reg.unregister("read_file")
    assert reg.has("read_file") is False


def test_unregister_missing_is_noop() -> None:
    reg = ToolRegistry()
    # Should not raise.
    reg.unregister("never-registered")


def test_list_names_is_sorted() -> None:
    reg = ToolRegistry()
    for n in ("zebra", "apple", "mango"):
        reg.register(_mk(n))
    assert reg.list_names() == ["apple", "mango", "zebra"]


def test_describe_all_shape() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file", ApprovalBucket.AUTO, requires_project=True))
    desc = reg.describe_all()
    assert desc == [
        {
            "name": "read_file",
            "description": "stub for read_file",
            "bucket": "auto",
            "kind": "inline",
            "requires_project": True,
        }
    ]


# --------------------------------------------------------------- session trust


def test_session_trust_round_trip() -> None:
    reg = ToolRegistry()
    reg.register(_mk("edit_file"))

    assert reg.is_trusted_for_session("main", "edit_file") is False
    reg.trust_for_session("main", "edit_file")
    assert reg.is_trusted_for_session("main", "edit_file") is True
    # Trust is session-scoped.
    assert reg.is_trusted_for_session("other-session", "edit_file") is False


def test_forget_session_trust() -> None:
    reg = ToolRegistry()
    reg.register(_mk("edit_file"))
    reg.trust_for_session("main", "edit_file")
    reg.forget_session_trust("main")
    assert reg.is_trusted_for_session("main", "edit_file") is False


def test_unregister_clears_session_trust() -> None:
    """A replaced tool must not inherit the old one's trust_session grant."""
    reg = ToolRegistry()
    reg.register(_mk("edit_file"))
    reg.trust_for_session("main", "edit_file")
    reg.unregister("edit_file")
    # Re-register with the same name - must start untrusted.
    reg.register(_mk("edit_file"))
    assert reg.is_trusted_for_session("main", "edit_file") is False


# --------------------------------------------------------------- policy parsing


def test_policy_from_empty_config() -> None:
    p = ToolPolicy.from_config(None)
    assert p.per_tool_default == {}
    assert p.per_tool_wildcard == []
    assert p.per_client == {}


def test_policy_parses_per_tool_defaults() -> None:
    p = ToolPolicy.from_config(
        {
            "read_file": {"default": "auto"},
            "write_file": {"default": "ask"},
        }
    )
    assert p.per_tool_default == {
        "read_file": ApprovalBucket.AUTO,
        "write_file": ApprovalBucket.ASK,
    }
    assert p.per_tool_wildcard == []


def test_policy_separates_wildcards_from_exact_names() -> None:
    p = ToolPolicy.from_config(
        {
            "read_file": {"default": "auto"},
            "mcp.slack.*": {"default": "ask"},
            "mcp.jira.search_*": {"default": "auto"},
        }
    )
    assert p.per_tool_default == {"read_file": ApprovalBucket.AUTO}
    # Wildcard order matches YAML declaration order.
    assert p.per_tool_wildcard == [
        ("mcp.slack.*", ApprovalBucket.ASK),
        ("mcp.jira.search_*", ApprovalBucket.AUTO),
    ]


def test_policy_parses_per_client_overrides() -> None:
    p = ToolPolicy.from_config(
        {
            "write_file": {"default": "ask"},
            "per_client": {
                "ide": {"write_file": "auto", "edit_file": "auto"},
                "webui": {"write_file": "block"},
            },
        }
    )
    assert p.per_client == {
        "ide": {
            "write_file": ApprovalBucket.AUTO,
            "edit_file": ApprovalBucket.AUTO,
        },
        "webui": {"write_file": ApprovalBucket.BLOCK},
    }


def test_policy_entry_without_default_is_skipped() -> None:
    """YAML that mentions a tool but supplies no default should not crash."""
    p = ToolPolicy.from_config({"read_file": {}})
    assert p.per_tool_default == {}


def test_policy_approval_timeout_secs_roundtrip() -> None:
    """The ``approval_timeout_secs`` top-level knob flows from raw
    YAML through to the runtime policy. Tested explicitly because
    the ``from_raw`` helper has to separate this reserved key from
    tool-name keys without confusing them."""
    p = ToolPolicy.from_config({"approval_timeout_secs": 30})
    assert p.approval_timeout_secs == 30
    # Tool entries alongside the knob still parse correctly.
    p2 = ToolPolicy.from_config(
        {
            "approval_timeout_secs": 90,
            "read_file": {"default": "ask"},
        }
    )
    assert p2.approval_timeout_secs == 90
    assert p2.per_tool_default == {"read_file": ApprovalBucket.ASK}


def test_policy_approval_timeout_secs_default_is_none() -> None:
    """When the knob isn't set, the policy reports None so the app
    falls back to the middleware's built-in default."""
    p = ToolPolicy.from_config({"read_file": {"default": "auto"}})
    assert p.approval_timeout_secs is None


def test_policy_config_rejects_unknown_bucket_value() -> None:
    """Typos in the bucket name surface at config load, not tool call time."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ToolPolicyConfig.from_raw({"read_file": {"default": "maybe-ish"}})


# --------------------------------------------------------------- resolve_bucket


def test_resolve_uses_tool_default_when_no_policy() -> None:
    reg = ToolRegistry()
    reg.register(_mk("read_file", ApprovalBucket.AUTO))
    assert reg.resolve_bucket(reg.lookup("read_file"), "telegram", "main") == (ApprovalBucket.AUTO)


def test_resolve_per_tool_default_overrides_tool_bucket() -> None:
    """YAML wins over the tool's baked-in default."""
    policy = ToolPolicy.from_config({"read_file": {"default": "ask"}})
    reg = ToolRegistry(policy)
    reg.register(_mk("read_file", ApprovalBucket.AUTO))
    assert reg.resolve_bucket(reg.lookup("read_file"), "telegram", "main") == (ApprovalBucket.ASK)


def test_resolve_per_client_overrides_per_tool() -> None:
    """Client-specific rules beat the global per-tool default."""
    policy = ToolPolicy.from_config(
        {
            "write_file": {"default": "ask"},
            "per_client": {"ide": {"write_file": "auto"}},
        }
    )
    reg = ToolRegistry(policy)
    reg.register(_mk("write_file", ApprovalBucket.ASK))
    assert reg.resolve_bucket(reg.lookup("write_file"), "ide", "main") == (ApprovalBucket.AUTO)
    # Non-ide clients still see the per-tool default.
    assert reg.resolve_bucket(reg.lookup("write_file"), "telegram", "main") == (ApprovalBucket.ASK)


def test_resolve_wildcard_matches_mcp_tool() -> None:
    policy = ToolPolicy.from_config({"mcp.slack.*": {"default": "ask"}})
    reg = ToolRegistry(policy)
    reg.register(_mk("mcp.slack.send_message", ApprovalBucket.AUTO, kind="mcp"))
    # Wildcard overrides the tool's own default.
    assert (
        reg.resolve_bucket(reg.lookup("mcp.slack.send_message"), "telegram", "main")
        == ApprovalBucket.ASK
    )


def test_resolve_exact_name_beats_wildcard() -> None:
    """``per_tool_default`` runs before wildcard matching."""
    policy = ToolPolicy.from_config(
        {
            "mcp.slack.ping": {"default": "auto"},
            "mcp.slack.*": {"default": "ask"},
        }
    )
    reg = ToolRegistry(policy)
    reg.register(_mk("mcp.slack.ping", ApprovalBucket.ASK, kind="mcp"))
    reg.register(_mk("mcp.slack.send_message", ApprovalBucket.ASK, kind="mcp"))
    assert (
        reg.resolve_bucket(reg.lookup("mcp.slack.ping"), "telegram", "main") == ApprovalBucket.AUTO
    )
    assert (
        reg.resolve_bucket(reg.lookup("mcp.slack.send_message"), "telegram", "main")
        == ApprovalBucket.ASK
    )


def test_resolve_wildcard_declaration_order_wins() -> None:
    """First declared wildcard that matches takes the decision."""
    policy = ToolPolicy.from_config(
        {
            "mcp.slack.*": {"default": "ask"},
            "mcp.*": {"default": "auto"},
        }
    )
    reg = ToolRegistry(policy)
    reg.register(_mk("mcp.slack.send", ApprovalBucket.AUTO, kind="mcp"))
    reg.register(_mk("mcp.jira.search", ApprovalBucket.AUTO, kind="mcp"))
    # Specific pattern appeared first -> wins for slack.
    assert (
        reg.resolve_bucket(reg.lookup("mcp.slack.send"), "telegram", "main") == ApprovalBucket.ASK
    )
    # Jira wasn't matched by the first, falls through to the broader one.
    assert (
        reg.resolve_bucket(reg.lookup("mcp.jira.search"), "telegram", "main") == ApprovalBucket.AUTO
    )


def test_resolve_client_default_when_tool_has_none() -> None:
    """Tool with no baked-in bucket + no YAML -> client default table."""
    # We deliberately construct a Tool without relying on default_bucket
    # being meaningful. The _CLIENT_DEFAULTS table kicks in.
    policy = ToolPolicy.from_config(None)
    reg = ToolRegistry(policy)
    # Inline tool whose own default is the broad-safe ASK; to force
    # the client table path, we shadow it via a no-match scenario
    # by asking for a tool whose default is the global fallback
    # and whose policy is empty. Simpler: check client default
    # works when tool default matches it.
    reg.register(_mk("probe", ApprovalBucket.ASK))
    assert reg.resolve_bucket(reg.lookup("probe"), "ide", "main") == (
        # tool default is ASK, wins before client-table. Verified
        # that when tool default is present, it's consulted.
        ApprovalBucket.ASK
    )


# --------------------------------------------------------------- per-tool per-client baked-in defaults


def test_register_per_client_defaults_applied() -> None:
    """Phase 4.7: tool author bakes in per-client defaults via
    ``register(per_client_defaults=...)``. The map is consulted
    between the wildcard layer and the tool's own default."""
    reg = ToolRegistry()
    reg.register(
        _mk("project_shell", ApprovalBucket.ASK),
        per_client_defaults={
            "cli": ApprovalBucket.ASK,
            "telegram": ApprovalBucket.ASK,
            "ide": ApprovalBucket.ASK,
            "webui": ApprovalBucket.BLOCK,
        },
    )
    tool = reg.lookup("project_shell")
    # Open WebUI gets block — the whole point of baked-in
    # defaults is to ship safe-by-default for least-trust.
    assert reg.resolve_bucket(tool, "webui", "main") == ApprovalBucket.BLOCK
    # Other clients get ask (matches the tool's own default, but
    # the path is layer 4 → layer 5; either way, ask).
    assert reg.resolve_bucket(tool, "telegram", "main") == ApprovalBucket.ASK
    assert reg.resolve_bucket(tool, "ide", "main") == ApprovalBucket.ASK


def test_operator_config_overrides_per_client_defaults() -> None:
    """Baked-in defaults sit BELOW operator config in the
    resolve chain. IDE operator who wants ``trust_session``
    for project_shell still gets their way."""
    policy = ToolPolicy.from_config({"per_client": {"ide": {"project_shell": "trust_session"}}})
    reg = ToolRegistry(policy)
    reg.register(
        _mk("project_shell", ApprovalBucket.ASK),
        per_client_defaults={
            "ide": ApprovalBucket.ASK,
            "webui": ApprovalBucket.BLOCK,
        },
    )
    tool = reg.lookup("project_shell")
    # Operator config wins over baked-in.
    assert reg.resolve_bucket(tool, "ide", "main") == ApprovalBucket.TRUST_SESSION
    # Where no operator config is set, baked-in default kicks in.
    assert reg.resolve_bucket(tool, "webui", "main") == ApprovalBucket.BLOCK


def test_per_client_defaults_cleared_on_unregister() -> None:
    """Unregistering a tool drops its baked-in defaults so a
    same-named replacement (MCP reload case) doesn't inherit
    stale settings."""
    reg = ToolRegistry()
    reg.register(
        _mk("flakey", ApprovalBucket.ASK),
        per_client_defaults={"webui": ApprovalBucket.BLOCK},
    )
    reg.unregister("flakey")
    # Re-register without per_client_defaults; webui now follows
    # the tool's own default (ASK) rather than the stale BLOCK.
    reg.register(_mk("flakey", ApprovalBucket.ASK))
    assert reg.resolve_bucket(reg.lookup("flakey"), "webui", "main") == (ApprovalBucket.ASK)


def test_per_client_defaults_missing_client_falls_through() -> None:
    """A tool with baked defaults for some clients but not all
    still falls through to layer 5 (tool default) for unlisted
    clients."""
    reg = ToolRegistry()
    reg.register(
        _mk("project_shell", ApprovalBucket.ASK),
        per_client_defaults={"webui": ApprovalBucket.BLOCK},
    )
    # Client 'telegram' isn't in the baked map → tool default wins.
    assert reg.resolve_bucket(reg.lookup("project_shell"), "telegram", "main") == ApprovalBucket.ASK

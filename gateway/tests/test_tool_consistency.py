"""Tests for the boot-time tool-schema consistency lint.

Pure checks over synthetic Tools — including the git_commit false-positive
guard (a `message` field outside the text-payload family must NOT be
flagged) — plus an integration assertion that the *real* inline registry
is payload-consistent after the 2026-07-01 cron rename."""

from __future__ import annotations

from typing import Any

from gateway.tool_consistency import check_tool_consistency
from gateway.tools import ApprovalBucket, Tool


async def _noop(_args: dict[str, Any], _ctx: Any) -> Any:  # pragma: no cover - never called
    raise AssertionError("consistency lint must not invoke tools")


def _tool(
    name: str,
    *,
    properties: dict[str, Any],
    required: list[str] | None = None,
    description: str = "d",
) -> Tool:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }
    return Tool(
        name=name,
        description=description,
        schema=schema,
        callable=_noop,
        default_bucket=ApprovalBucket.AUTO,
    )


def _payload(name: str, field: str) -> Tool:
    return _tool(name, properties={field: {"type": "string"}}, required=[field])


# --------------------------------------------------------------- payload naming


def test_flags_family_tool_using_off_canonical_name() -> None:
    """A text-payload-family tool (cron_add) using `message` instead of the
    canonical `text` is flagged."""
    tools = [_payload("send_message", "text"), _payload("cron_add", "message")]
    warnings = check_tool_consistency(tools)
    joined = " ".join(warnings)
    assert "cron_add" in joined
    assert "message" in joined and "text" in joined


def test_family_all_canonical_no_warning() -> None:
    tools = [_payload("send_message", "text"), _payload("learn_add", "text")]
    assert check_tool_consistency(tools) == []


def test_non_family_message_field_not_flagged() -> None:
    """The git_commit false-positive guard: a `message` field on a tool
    OUTSIDE the text-payload family (a commit message — a different
    concept) must NOT be flagged. This is the exact false positive a
    blanket synonym scan produces and this rule avoids."""
    tools = [_payload("send_message", "text"), _payload("git_commit", "message")]
    assert check_tool_consistency(tools) == []


def test_family_tool_already_canonical_no_warning() -> None:
    assert check_tool_consistency([_payload("cron_add", "text")]) == []


# --------------------------------------------------------------- descriptions


def test_flags_missing_description() -> None:
    tool = _tool("mystery", properties={"id": {"type": "string"}}, description="  ")
    warnings = check_tool_consistency([tool])
    assert any("mystery" in w and "no description" in w for w in warnings)


# --------------------------------------------------------------- clean / empty


def test_clean_registry_no_warnings() -> None:
    tools = [
        _payload("send_message", "text"),
        _payload("learn_add", "text"),
        _tool("read_file", properties={"path": {"type": "string"}}, required=["path"]),
    ]
    assert check_tool_consistency(tools) == []


def test_empty_registry_no_warnings() -> None:
    assert check_tool_consistency([]) == []


# --------------------------------------------------------------- integration


def test_real_inline_registry_is_payload_consistent(tmp_path: Any) -> None:
    """Regression guard for the 2026-07-01 rename: the shipped inline
    text-payload family now agrees on `text`, and git_commit's `message`
    is (correctly) not swept in — so the lint raises no payload warning
    against the real registry."""
    from fastapi.testclient import TestClient

    from gateway.app import create_app

    from ._fixtures import build_test_config

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    TestClient(app)  # ensure state is wired

    warnings = check_tool_consistency(app.state.tool_registry.list_all())
    assert not any("text-payload family" in w for w in warnings)

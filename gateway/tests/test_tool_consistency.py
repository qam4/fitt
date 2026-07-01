"""Tests for the boot-time tool-schema consistency lint.

Pure checks over synthetic Tools, plus an integration assertion that the
*real* inline registry trips the payload-naming rule (cron_add uses
`message`, send_message/learn_add use `text`) — the bug the check
exists to catch."""

from __future__ import annotations

from typing import Any

from gateway.tool_consistency import check_tool_consistency
from gateway.tools import ApprovalBucket, Tool


async def _noop(_args: dict[str, Any], _ctx: Any) -> Any:  # pragma: no cover - never called
    raise AssertionError("consistency lint must not invoke tools")


def _tool(
    name: str, *, properties: dict[str, Any], required: list[str], description: str = "d"
) -> Tool:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
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


def test_flags_inconsistent_payload_naming() -> None:
    tools = [_payload("send_message", "text"), _payload("cron_add", "message")]
    warnings = check_tool_consistency(tools)
    joined = " ".join(warnings)
    assert "disagree" in joined
    assert "text" in joined and "message" in joined
    assert "send_message" in joined and "cron_add" in joined


def test_consistent_payload_naming_no_warning() -> None:
    tools = [_payload("send_message", "text"), _payload("learn_add", "text")]
    assert check_tool_consistency(tools) == []


def test_single_payload_variant_no_warning() -> None:
    assert check_tool_consistency([_payload("only", "message")]) == []


def test_non_payload_fields_ignored() -> None:
    """Fields outside a synonym group never trip the payload rule."""
    tools = [
        _tool("a", properties={"id": {"type": "string"}}, required=["id"]),
        _tool("b", properties={"path": {"type": "string"}}, required=["path"]),
    ]
    assert check_tool_consistency(tools) == []


# --------------------------------------------------------------- required surface


def test_flags_heavy_required_surface() -> None:
    heavy = _tool(
        "edit_file",
        properties={k: {"type": "string"} for k in ("path", "old", "new", "occurrence")},
        required=["path", "old", "new", "occurrence"],
    )
    warnings = check_tool_consistency([heavy])
    assert any("edit_file" in w and "4 fields" in w for w in warnings)


def test_required_at_threshold_is_ok() -> None:
    ok = _tool(
        "three",
        properties={k: {"type": "string"} for k in ("a", "b", "c")},
        required=["a", "b", "c"],
    )
    assert check_tool_consistency([ok]) == []


# --------------------------------------------------------------- descriptions


def test_flags_missing_description() -> None:
    tool = _tool("mystery", properties={"id": {"type": "string"}}, required=[], description="  ")
    warnings = check_tool_consistency([tool])
    assert any("mystery" in w and "no description" in w for w in warnings)


# --------------------------------------------------------------- clean / empty


def test_clean_registry_no_warnings() -> None:
    tools = [
        _tool("a", properties={"text": {"type": "string"}}, required=["text"]),
        _tool("b", properties={"text": {"type": "string"}}, required=["text"]),
    ]
    assert check_tool_consistency(tools) == []


def test_empty_registry_no_warnings() -> None:
    assert check_tool_consistency([]) == []


# --------------------------------------------------------------- integration


def test_real_inline_registry_flags_payload_inconsistency(tmp_path: Any) -> None:
    """The shipped inline tools genuinely disagree (`message` vs `text`),
    so the lint flags it against the real registry — the motivating bug."""
    from fastapi.testclient import TestClient

    from gateway.app import create_app

    from ._fixtures import build_test_config

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    TestClient(app)  # ensure state is wired

    warnings = check_tool_consistency(app.state.tool_registry.list_all())
    assert any("disagree" in w and "message" in w and "text" in w for w in warnings)

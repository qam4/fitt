"""Tests for ``record_gap`` — the capability-gap logger.

Focused on the 2026-05-11 suppression-on-known-tool behaviour.
The underlying ``parse_gap`` is covered in ``test_capabilities``;
this file pins the decision of whether to *append* the parsed
gap to the log based on whether the tool registry already has
the suggested tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.agent_loop import record_gap
from gateway.capabilities import CapabilityGapLog
from gateway.tools import ApprovalBucket, Tool, ToolContext, ToolRegistry, ToolResult


async def _noop(args: dict[str, Any], ctx: ToolContext) -> ToolResult:  # pragma: no cover
    raise AssertionError("not invoked in these tests")


def _mk_registry(*names: str) -> ToolRegistry:
    reg = ToolRegistry()
    for name in names:
        reg.register(
            Tool(
                name=name,
                description=f"stub {name}",
                schema={"type": "object", "properties": {}},
                callable=_noop,
                default_bucket=ApprovalBucket.AUTO,
                kind="inline",
                requires_project=False,
            )
        )
    return reg


def test_record_gap_appends_when_registry_missing_tool(tmp_path: Path) -> None:
    """No registry passed → behaves as before: any gap gets
    appended. This preserves the original contract for paths
    that don't plumb a registry through (currently none; future
    callers shouldn't break if they forget)."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    reply = "I'd need a tool to fetch a web page. Consider adding http_get."
    record_gap(log, reply, "main")
    gaps = log.read()
    assert len(gaps) == 1
    assert gaps[0].action == "fetch a web page"


def test_record_gap_appends_when_suggested_tool_not_registered(
    tmp_path: Path,
) -> None:
    """Registry is passed but has no ``http_get`` tool — this is
    a genuine capability gap, the log gets the entry."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file", "list_directory")
    reply = "I'd need a tool to fetch a web page. Consider adding http_get."
    record_gap(log, reply, "main", tool_registry=registry)
    assert len(log.read()) == 1


def test_record_gap_suppresses_when_suggested_tool_is_registered(
    tmp_path: Path,
) -> None:
    """The 2026-05-10 regression guard. The model falls back to
    the gap-reporter phrasing for a tool that actually exists
    (typically because a *different* bug made tool calls fail
    on argument errors). Registry check catches this and skips
    the log append so the capability-gap log stays a trustworthy
    backlog of real gaps."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file", "http_get")
    reply = "I'd need a tool to read a file. Consider adding `read_file`."
    record_gap(log, reply, "main", tool_registry=registry)
    assert log.read() == []


def test_record_gap_suppresses_when_suggestion_has_backticks(
    tmp_path: Path,
) -> None:
    """Models routinely wrap the suggested tool name in
    backticks ("Consider adding ``read_file``"). The filter
    must strip punctuation/backticks before comparing against
    the registry."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file")
    reply = "I'd need a tool to read a file. Consider adding `read_file`."
    record_gap(log, reply, "main", tool_registry=registry)
    assert log.read() == []


def test_record_gap_suppresses_when_suggestion_embeds_registered_tool(
    tmp_path: Path,
) -> None:
    """Models also phrase suggestions conversationally:
    "Consider adding a read_file tool." or "consider a
    read_file capability." Filter tokenises the suggestion and
    matches any word against the registry."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file")
    reply = "I'd need a tool to read a file. Consider adding a read_file tool."
    record_gap(log, reply, "main", tool_registry=registry)
    assert log.read() == []


def test_record_gap_appends_when_no_suggestion(tmp_path: Path) -> None:
    """Short-form gap ("I'd need a tool to X.") with no
    ``Consider adding`` half can't be filtered — we don't
    know which tool was implied. Log it; false-negatives
    (missed suppression) are preferable to false-positives
    (suppressing real gap reports that just happened not to
    include a suggestion)."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file")
    reply = "I'd need a tool to read a file."
    record_gap(log, reply, "main", tool_registry=registry)
    assert len(log.read()) == 1


def test_record_gap_registry_check_is_case_insensitive(tmp_path: Path) -> None:
    """Tool names are lowercase by convention but the model
    might capitalise them in prose ("Consider adding Read_file").
    Filter is case-insensitive."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file")
    reply = "I'd need a tool to read a file. Consider adding Read_File."
    record_gap(log, reply, "main", tool_registry=registry)
    assert log.read() == []


def test_record_gap_no_reply_does_nothing(tmp_path: Path) -> None:
    """Empty reply is a no-op; registry presence doesn't
    change that."""
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry("read_file")
    record_gap(log, "", "main", tool_registry=registry)
    assert log.read() == []


def test_record_gap_null_log_does_nothing(tmp_path: Path) -> None:
    """Gap logging is best-effort; a missing log is fine."""
    registry = _mk_registry("read_file")
    # Should not raise.
    record_gap(None, "I'd need a tool to X. Consider adding X.", "main", tool_registry=registry)


@pytest.mark.parametrize(
    "reply,registry_tools,expected_len",
    [
        # Real gap, tool not registered — append.
        (
            "I'd need a tool to send SMS messages. Consider adding send_sms.",
            ("read_file",),
            1,
        ),
        # Tool IS registered — suppress.
        (
            "I'd need a tool to send SMS messages. Consider adding send_sms.",
            ("send_sms",),
            0,
        ),
        # Two suggestions, at least one is registered — suppress.
        # (Current implementation: any token match suppresses. We
        # accept that as safe default; better to lose a real gap
        # than pollute.)
        (
            "I'd need a tool to read and edit files. Consider adding read_file or edit_file.",
            ("read_file",),
            0,
        ),
    ],
)
def test_record_gap_parametrised_matrix(
    tmp_path: Path,
    reply: str,
    registry_tools: tuple[str, ...],
    expected_len: int,
) -> None:
    log = CapabilityGapLog(tmp_path / "gaps.log")
    registry = _mk_registry(*registry_tools)
    record_gap(log, reply, "main", tool_registry=registry)
    assert len(log.read()) == expected_len

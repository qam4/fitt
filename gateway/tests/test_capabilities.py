"""Tests for capability block + gap logging.

Three concerns:

* ``build_capability_block`` produces a readable prompt section
  with each registered tool; gracefully handles empty/huge
  registries via truncation.
* ``parse_gap`` finds the standard gap phrasing across the
  variations the model actually emits (straight vs. curly
  apostrophe, "I'd" vs "I would", suggestion present vs
  absent) and *doesn't* flag non-gap sentences that happen to
  contain the words.
* ``CapabilityGapLog`` appends and reads back GapReports; the
  ``rank_gaps`` helper groups by canonical action text and
  orders by frequency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gateway.capabilities import (
    CapabilityGapLog,
    GapReport,
    build_capability_block,
    parse_gap,
    rank_gaps,
)
from gateway.tools import ApprovalBucket, Tool, ToolRegistry

# --------------------------------------------------------------- helpers


async def _noop(args: dict[str, Any], ctx: Any) -> Any:  # pragma: no cover
    raise AssertionError("not invoked in these tests")


def _mk_tool(name: str, description: str = "") -> Tool:
    return Tool(
        name=name,
        description=description or f"stub {name}",
        schema={"type": "object", "properties": {}},
        callable=_noop,
        default_bucket=ApprovalBucket.AUTO,
        requires_project=False,
        kind="inline",
    )


# --------------------------------------------------------------- block


def test_build_block_lists_registered_tools() -> None:
    reg = ToolRegistry()
    reg.register(_mk_tool("read_file", "Read a file from a project."))
    reg.register(_mk_tool("git_status", "Show the git status."))
    block = build_capability_block(reg)
    assert "[Capabilities]" in block
    assert "`read_file`" in block
    assert "Read a file from a project." in block
    assert "`git_status`" in block
    # Instruction block for gap reporting is present.
    assert "I'd need a tool to" in block


def test_build_block_handles_empty_registry() -> None:
    reg = ToolRegistry()
    block = build_capability_block(reg)
    assert "no tools registered" in block


def test_build_block_truncates_many_tools() -> None:
    """A pathological 100-tool registry gets capped at a sensible
    size, with a truncation note that points at list_capabilities."""
    reg = ToolRegistry()
    for i in range(100):
        reg.register(_mk_tool(f"tool_{i}"))
    block = build_capability_block(reg)
    assert "more;" in block or "truncated" in block
    assert "list_capabilities" in block


def test_build_block_size_capped() -> None:
    """Even with ~10 tools bearing huge descriptions, the block
    stays within the hard size cap."""
    reg = ToolRegistry()
    for i in range(10):
        reg.register(_mk_tool(f"tool_{i}", "A " * 500))
    block = build_capability_block(reg)
    # _MAX_BLOCK_CHARS is 4000; allow a small overrun for the
    # trailer computation.
    assert len(block) <= 4200


# --------------------------------------------------------------- parse_gap


def test_parse_gap_basic() -> None:
    reply = "I'd need a tool to fetch a web page. Consider adding http_get."
    got = parse_gap(reply)
    assert got is not None
    assert got.action == "fetch a web page"
    assert got.suggestion == "http_get"


def test_parse_gap_without_suggestion() -> None:
    """Short form: the 'Consider adding' half is optional."""
    reply = "I'd need a tool to run a background job."
    got = parse_gap(reply)
    assert got is not None
    assert got.action == "run a background job"
    assert got.suggestion == ""


def test_parse_gap_accepts_curly_apostrophe() -> None:
    reply = "I\u2019d need a tool to open a PR. Consider adding git_pr_create."
    got = parse_gap(reply)
    assert got is not None
    assert got.action == "open a PR"
    assert got.suggestion == "git_pr_create"


def test_parse_gap_accepts_i_would() -> None:
    reply = "I would need a tool to tail a log file."
    got = parse_gap(reply)
    assert got is not None
    assert got.action == "tail a log file"


def test_parse_gap_finds_mid_sentence() -> None:
    """The gap phrase need not be at the start of the reply;
    models often say 'Happy to help! I'd need a tool to ...'"""
    reply = "Happy to help with that. I'd need a tool to query Postgres. Consider adding db_query."
    got = parse_gap(reply)
    assert got is not None
    assert got.action == "query Postgres"


def test_parse_gap_returns_none_on_no_match() -> None:
    assert parse_gap("Here's the answer: 42.") is None
    assert parse_gap("") is None
    assert parse_gap("   ") is None


def test_parse_gap_does_not_match_unrelated_words() -> None:
    """Sanity: a sentence mentioning 'tool' in passing shouldn't
    flag as a gap."""
    assert parse_gap("I used the grep_repo tool to find that. The codebase has 5 files.") is None


# --------------------------------------------------------------- log


def test_log_append_and_read(tmp_path: Path) -> None:
    log = CapabilityGapLog(tmp_path / "gaps.log")
    log.append(GapReport(ts=100.0, session_key="main", action="fetch url", suggestion="http_get"))
    log.append(GapReport(ts=200.0, session_key="main", action="run cron", suggestion=""))
    gaps = log.read()
    assert len(gaps) == 2
    assert gaps[0].action == "fetch url"
    assert gaps[1].action == "run cron"


def test_log_read_filters_by_since(tmp_path: Path) -> None:
    log = CapabilityGapLog(tmp_path / "gaps.log")
    log.append(GapReport(ts=100.0, session_key="main", action="a", suggestion=""))
    log.append(GapReport(ts=200.0, session_key="main", action="b", suggestion=""))
    log.append(GapReport(ts=300.0, session_key="main", action="c", suggestion=""))
    recent = log.read(since=150.0)
    assert [g.action for g in recent] == ["b", "c"]


def test_log_read_empty_when_missing_file(tmp_path: Path) -> None:
    log = CapabilityGapLog(tmp_path / "nope.log")
    assert log.read() == []


def test_log_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "gaps.log"
    path.write_text(
        '{"ts": 100.0, "session_key": "main", "action": "valid", "suggestion": ""}\n'
        "garbage not json\n"
        '{"ts": 200.0, "session_key": "main", "action": "also valid", "suggestion": ""}\n'
    )
    log = CapabilityGapLog(path)
    gaps = log.read()
    assert [g.action for g in gaps] == ["valid", "also valid"]


# --------------------------------------------------------------- rank


def test_rank_groups_by_canonical_action() -> None:
    """Case and whitespace differences dedupe."""
    gaps = [
        GapReport(ts=100.0, session_key="main", action="fetch a url", suggestion=""),
        GapReport(ts=200.0, session_key="main", action="Fetch a URL", suggestion=""),
        GapReport(ts=300.0, session_key="main", action="fetch  a  url", suggestion=""),
        GapReport(ts=150.0, session_key="main", action="run tests", suggestion=""),
    ]
    ranked = rank_gaps(gaps)
    # Most frequent first.
    assert ranked[0][0] == "fetch a url"
    assert ranked[0][1] == 3
    # The 'most recent' of the group is the latest ts.
    assert ranked[0][2].ts == 300.0


def test_rank_ties_broken_by_recency() -> None:
    gaps = [
        GapReport(ts=100.0, session_key="main", action="a", suggestion=""),
        GapReport(ts=500.0, session_key="main", action="b", suggestion=""),
    ]
    ranked = rank_gaps(gaps)
    # Same count (1 each); newer first.
    assert ranked[0][0] == "b"
    assert ranked[1][0] == "a"


def test_rank_empty() -> None:
    assert rank_gaps([]) == []

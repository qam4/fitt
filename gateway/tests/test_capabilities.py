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
    detect_narrated_tool_call,
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


def test_build_block_includes_current_time_preamble() -> None:
    """Regression guard for the 2026-05-07 "model picked 13:00 UTC
    for a 1 PM EDT reminder" issue. Without a current-time
    preamble the model reasons in UTC by default and emits naive
    ISO timestamps that land in the past (or the wrong time of
    day) when interpreted.

    The preamble gives the model three usable shapes: the local
    wall clock for human phrasing, a UTC offset for emitting
    timezone-aware ISO strings, and an explicit UTC ISO for math.
    We don't pin the exact format because Windows vs Linux tzname
    varies ("Eastern Daylight Time" vs "EDT") — just assert the
    anchor and at least one concrete piece."""
    reg = ToolRegistry()
    reg.register(_mk_tool("read_file", "Read a file."))
    block = build_capability_block(reg)
    assert "[Current time]" in block
    # Comes before the capabilities list so the model sees the
    # clock before any tools.
    assert block.index("[Current time]") < block.index("[Capabilities]")
    # At least one of the three shapes landed. UTC offset is the
    # most portable to assert on (YYYY-MM-DDTHH:MM:SS+00:00).
    assert "+00:00" in block


def test_build_block_explains_approval_ux() -> None:
    """Regression guard for the 2026-05-07 "model invents
    confirmation rituals" issue.

    Without an approval-UX section in the system prompt, models
    that hit an ``ask`` tool go silent from their own perspective
    (the tool doesn't return while awaiting human approval) and
    fill the gap by narrating a fake procedure — observed live as
    "type 'Approve: cron_ad...' to confirm". The capability
    block must tell the model that approvals are surfaced by the
    client's own UI and discourage typed-confirmation rituals."""
    reg = ToolRegistry()
    reg.register(_mk_tool("cron_add", "Schedule a cron."))
    block = build_capability_block(reg)
    assert "[How tool calls work]" in block
    # Name the three resolution outcomes so the model knows what
    # comes back to it.
    assert "Approve" in block
    assert "Reject" in block
    assert "Trust session" in block
    # And explicitly discourage the hallucinated ritual.
    assert "typing" in block.lower() or "paste" in block.lower()


def test_build_block_keeps_trailer_even_when_truncated() -> None:
    """When tool count overflows the hard-cap budget, we still
    need the model to see both the approval-UX note AND the
    gap-report instruction — losing either one causes the
    failure modes those sections exist to prevent. The truncation
    code trims the TOOL LIST, not the trailer."""
    reg = ToolRegistry()
    for i in range(10):
        reg.register(_mk_tool(f"tool_{i}", "A " * 500))
    block = build_capability_block(reg)
    assert "[How tool calls work]" in block
    assert "I'd need a tool to" in block


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


def test_parse_gap_strips_backticks_around_action() -> None:
    """Regression: the model routinely wraps filenames in backticks
    (``read `README.md```). Those backticks must not leak into the
    canonicalised action, or similar gap reports fail to dedupe in
    ``rank_gaps`` and the log entries render with a stray
    backtick."""
    reply = "I'd need a tool to read the `README.md`. Consider adding `read_file`."
    got = parse_gap(reply)
    assert got is not None
    assert "`" not in got.action
    assert "`" not in got.suggestion
    # And dedupes with the no-backticks form.
    other = parse_gap("I'd need a tool to read the README.md.")
    assert other is not None
    assert got.action.rstrip(".") == other.action.rstrip(".")


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


# --------------------------------------------------------------- detect_narrated_tool_call


def test_detect_narrated_tool_call_matches_exact_live_emission() -> None:
    """The exact emission pattern observed 2026-05-07 from
    qwen2.5-coder:14b during a cron firing. The model announces
    it will call a tool, then writes the tool JSON in a fenced
    block — but emits no real tool_calls structure."""
    reply = (
        "I'll create that cron now.\n"
        "\n"
        "```json\n"
        "{\n"
        '  "name": "cron_add",\n'
        '  "arguments": {\n'
        '    "schedule_spec": "at 2026-05-06T13:00:00",\n'
        '    "message": "It is time to go have lunch."\n'
        "  }\n"
        "}\n"
        "```"
    )
    got = detect_narrated_tool_call(reply)
    assert got is not None
    assert got.tool_name == "cron_add"
    assert "cron_add" in got.raw_fence


def test_detect_narrated_tool_call_matches_send_message_shape() -> None:
    """Same pattern, different tool. Another live emission."""
    reply = '```json\n{"name": "send_message", "arguments": {"text": "Test 7 works"}}\n```'
    got = detect_narrated_tool_call(reply)
    assert got is not None
    assert got.tool_name == "send_message"


def test_detect_narrated_tool_call_ignores_plain_reply() -> None:
    """Normal natural-language replies must not false-positive."""
    assert detect_narrated_tool_call("Done. I've scheduled that for 2 PM.") is None
    assert detect_narrated_tool_call("") is None


def test_detect_narrated_tool_call_ignores_code_fences_without_name_field() -> None:
    """Many model replies legitimately contain fenced JSON — a
    data dump, a config example, an illustrative snippet. These
    should not trip the detector.

    Rule: the fenced body must contain a top-level ``"name"``
    key that looks like a tool name. Data examples lack that."""
    reply = 'Here\'s the config shape:\n```json\n{"server": {"port": 8421}, "timezone": "UTC"}\n```'
    assert detect_narrated_tool_call(reply) is None


def test_detect_narrated_tool_call_returns_first_match_only() -> None:
    """A reply with two narrated calls (rare but possible)
    returns only the first. We can iterate on this rule if we
    see a reason to."""
    reply = (
        "```json\n"
        '{"name": "first_tool", "arguments": {}}\n'
        "```\n"
        "and also\n"
        "```json\n"
        '{"name": "second_tool", "arguments": {}}\n'
        "```"
    )
    got = detect_narrated_tool_call(reply)
    assert got is not None
    assert got.tool_name == "first_tool"


# ------------------------------ is_tool_use_expected_but_none


def test_expected_but_none_fires_on_narrated_jsonfence() -> None:
    """JSON-fenced narration from the 2026-05-07 incident —
    tools were offered, no real tool_calls, finish_reason=stop,
    substantive reply. Signal fires."""
    from gateway.capabilities import is_tool_use_expected_but_none

    reply = (
        "I'll create that cron now.\n"
        "\n"
        "```json\n"
        '{"name": "cron_add", "arguments": {"schedule_spec": "at 1 PM"}}\n'
        "```"
    )
    assert (
        is_tool_use_expected_but_none(
            reply,
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is True
    )


def test_expected_but_none_fires_on_sentinel_narration() -> None:
    """The 2026-05-08 sentinel shape — same invariants should
    fire regardless of specific narration format, which is the
    whole point of the shape-based check."""
    from gateway.capabilities import is_tool_use_expected_but_none

    reply = (
        "```tool TOOL_NAME: read_file BEGIN_ARG: project home-ai-cluster path README.md END_ARG ```"
    )
    assert (
        is_tool_use_expected_but_none(
            reply,
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is True
    )


def test_expected_but_none_fires_on_capability_false_negative() -> None:
    """The 2026-05-10 opening: "I can't provide weather
    forecasts" when http_get was in the capability block.
    No narration pattern at all, but tools were offered,
    reply was substantive, model chose not to use them.
    This is exactly the "silent stubborn model" case the
    shape-based check catches that the old regex missed."""
    from gateway.capabilities import is_tool_use_expected_but_none

    reply = (
        "I can't provide weather forecasts. For accurate "
        "predictions, I recommend checking the National "
        "Weather Service or your favorite weather app."
    )
    assert (
        is_tool_use_expected_but_none(
            reply,
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is True
    )


def test_expected_but_none_does_not_fire_without_tools_offered() -> None:
    """If the client didn't send ``tools``/``tool_choice``,
    the model was never asked to call anything. Plain chat
    with qwen3 doesn't trigger the signal no matter what
    the reply looks like."""
    from gateway.capabilities import is_tool_use_expected_but_none

    reply = "I can't provide weather forecasts because this is a stateless chat request."
    assert (
        is_tool_use_expected_but_none(
            reply,
            tools_were_offered=False,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is False
    )


def test_expected_but_none_does_not_fire_with_real_tool_calls() -> None:
    """The model DID call a tool AND narrated some extra prose.
    That's a chatty model, not a failed one. Short-circuit."""
    from gateway.capabilities import is_tool_use_expected_but_none

    reply = "Calling http_get now."
    assert (
        is_tool_use_expected_but_none(
            reply,
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=True,
        )
        is False
    )


def test_expected_but_none_does_not_fire_on_short_reply() -> None:
    """Legitimately short replies ("Done.", "Yes.") don't
    trigger the signal — they're plausibly just confirming
    a prior tool result, not substituting prose for a tool
    call. Threshold is 40 chars."""
    from gateway.capabilities import is_tool_use_expected_but_none

    assert (
        is_tool_use_expected_but_none(
            "Yes.",
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is False
    )
    assert (
        is_tool_use_expected_but_none(
            "Done, anything else?",
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is False
    )


def test_expected_but_none_does_not_fire_on_length_cutoff() -> None:
    """finish_reason=length means the model was cut off by
    a token limit, not by choice. That's a different problem
    (context too long) than "chose not to use a tool."
    Same for content_filter and other error-y finish reasons."""
    from gateway.capabilities import is_tool_use_expected_but_none

    long_reply = "a" * 100
    assert (
        is_tool_use_expected_but_none(
            long_reply,
            tools_were_offered=True,
            finish_reason="length",
            had_real_tool_calls=False,
        )
        is False
    )
    assert (
        is_tool_use_expected_but_none(
            long_reply,
            tools_were_offered=True,
            finish_reason="content_filter",
            had_real_tool_calls=False,
        )
        is False
    )


def test_expected_but_none_does_not_fire_on_empty_reply() -> None:
    """Empty reply — no narration, no action, nothing to
    detect. Likely a tool-loop-intermediate response the
    caller shouldn't have invoked us on."""
    from gateway.capabilities import is_tool_use_expected_but_none

    assert (
        is_tool_use_expected_but_none(
            "",
            tools_were_offered=True,
            finish_reason="stop",
            had_real_tool_calls=False,
        )
        is False
    )


def test_expected_but_none_accepts_end_turn_finish_reason() -> None:
    """Anthropic uses ``end_turn`` where OpenAI uses ``stop``.
    Same meaning: model finished cleanly. Both should trip
    the signal when the other conditions hold."""
    from gateway.capabilities import is_tool_use_expected_but_none

    long_reply = "a" * 100
    assert (
        is_tool_use_expected_but_none(
            long_reply,
            tools_were_offered=True,
            finish_reason="end_turn",
            had_real_tool_calls=False,
        )
        is True
    )


def test_expected_but_none_accepts_none_finish_reason() -> None:
    """Some providers / stubs leave finish_reason as None
    when the model stopped naturally. Treat it as stop.
    This is also the default in callers that don't plumb
    finish_reason through, which today is all of them."""
    from gateway.capabilities import is_tool_use_expected_but_none

    long_reply = "a" * 100
    assert (
        is_tool_use_expected_but_none(
            long_reply,
            tools_were_offered=True,
            finish_reason=None,
            had_real_tool_calls=False,
        )
        is True
    )

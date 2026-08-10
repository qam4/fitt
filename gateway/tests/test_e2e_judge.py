"""Judged-e2e harness — CliJudge tests (Phase B). Fake runner, no kiro-cli."""

from __future__ import annotations

import pytest

from gateway.e2e_eval import JudgeInput
from gateway.e2e_judge import CliJudge, build_judge_prompt, parse_verdict


def _ji(reply: str = "a reply", *, snapshot: dict | None = None) -> JudgeInput:
    return JudgeInput(
        intent="news_summary",
        rubric="Is the summary grounded and on-topic?",
        reply=reply,
        tool_sequence=("web_search:ok",),
        outcome_passed=True,
        outcome_reason="web_search fired",
        snapshot=snapshot or {},
    )


def _runner(out: str):
    async def _fn(argv, stdin_text):
        _fn.seen = (argv, stdin_text)
        return out

    return _fn


def _raising_runner(exc: Exception):
    async def _fn(argv, stdin_text):
        raise exc

    return _fn


# --------------------------------------------------------------- prompt


def test_prompt_includes_rubric_reply_tools_outcome() -> None:
    p = build_judge_prompt(_ji(reply="the market rose 2%"))
    assert "Is the summary grounded" in p
    assert "the market rose 2%" in p
    assert "web_search:ok" in p
    assert "PASS" in p  # objective outcome context


def test_prompt_grounds_judge_in_internals() -> None:
    """The judge prompt must carry the side-effect snapshot as ground
    truth (cron/todos/events) and instruct the judge to trust it over the
    reply's claims — so a reply that lies about what it did scores low."""
    snap = {
        "cron_jobs": [
            {"name": "reminder", "schedule_kind": "at", "message": "call doctor", "enabled": True}
        ],
        "todos_text": "## Open\n- [x] buy milk\n",
        "event_kinds": ["tool_call_executed", "turn_finished"],
    }
    p = build_judge_prompt(_ji(snapshot=snap))
    assert "GROUND TRUTH" in p
    assert "buy milk" in p  # todos side effect
    assert "call doctor" in p  # cron side effect
    assert "tool_call_executed" in p  # events


def test_prompt_marks_no_tools_when_empty() -> None:
    ji = JudgeInput(
        intent="chitchat",
        rubric="friendly?",
        reply="hi there",
        tool_sequence=(),
        outcome_passed=True,
        outcome_reason="no tool, replied",
    )
    assert "(no tools executed)" in build_judge_prompt(ji)


def test_prompt_renders_tool_call_args_and_results() -> None:
    """Tier 1: the judge sees each tool's args + result, not just the
    name — so it can check the RIGHT tool ran with the RIGHT args."""
    ji = JudgeInput(
        intent="reminder",
        rubric="did it set a reminder?",
        reply="done",
        tool_sequence=("cron_add:ok",),
        outcome_passed=True,
        outcome_reason="cron set",
        tool_calls=(
            {
                "name": "cron_add",
                "args": {"message": "call the doctor", "when": "2026-08-11T09:00"},
                "ok": True,
                "result": "scheduled cron abc123",
            },
        ),
    )
    p = build_judge_prompt(ji)
    assert "cron_add(" in p
    assert "call the doctor" in p  # args visible
    assert "scheduled cron abc123" in p  # result visible


def test_prompt_tier2_renders_timeline_and_asks_for_root_cause() -> None:
    """Tier 2: with a timeline the judge sees the per-iteration trace AND
    is asked to name the root cause — that's what turns 'it failed' into
    'it failed because iteration N re-emitted the same call'."""
    ji = JudgeInput(
        intent="todo",
        rubric="did it add the todo?",
        reply="",
        tool_sequence=("todo_add:ok", "todo_add:ok"),
        outcome_passed=False,
        outcome_reason="loop exhausted",
        loop_status="tool_loop_exhausted",
        timeline=(
            {
                "kind": "llm_call_completed",
                "iteration": 0,
                "out_tokens": 900,
                "tool_calls_count": 1,
            },
            {
                "kind": "tool_call_planned",
                "iteration": 0,
                "tool_name": "todo_add",
                "args": {"text": "x"},
            },
            {
                "kind": "tool_call_executed",
                "tool_name": "todo_add",
                "ok": True,
                "result_summary": "added",
            },
            {
                "kind": "llm_call_completed",
                "iteration": 1,
                "out_tokens": 900,
                "tool_calls_count": 1,
            },
            {
                "kind": "tool_call_planned",
                "iteration": 1,
                "tool_name": "todo_add",
                "args": {"text": "x"},
            },
        ),
    )
    p = build_judge_prompt(ji)
    assert "Turn timeline" in p
    assert "ROOT CAUSE" in p
    assert "tool_call_planned" in p
    assert "out_tokens=900" in p


def test_prompt_tier1_omits_timeline_section() -> None:
    """Standard detail stays lean — no timeline, no root-cause ask."""
    p = build_judge_prompt(_ji())
    assert "Turn timeline" not in p
    assert "ROOT CAUSE" not in p


def test_prompt_surfaces_loop_status_when_not_ok() -> None:
    ji = JudgeInput(
        intent="todo",
        rubric="did it add the todo?",
        reply="",
        tool_sequence=(),
        outcome_passed=False,
        outcome_reason="nothing added",
        loop_status="tool_loop_exhausted",
        error="did not terminate within 10 iterations",
    )
    p = build_judge_prompt(ji)
    assert "tool_loop_exhausted" in p
    assert "10 iterations" in p


# --------------------------------------------------------------- parse


def test_parse_truncated_json_does_not_invert_verdict() -> None:
    """Regression (2026-08-10): a truncated JSON verdict whose text says
    `"passed": false` must NEVER be scored PASS because the surrounding
    prose contains the word "PASS". A false pass is the worst eval bug —
    we refuse to guess and the caller records it un-judged."""
    raw = '> {"passed": false, "score": 0.2, "reasoning": "The objective outcome is PASS but the'
    with pytest.raises(ValueError):
        parse_verdict(raw)


def test_parse_strips_ansi_colour_codes() -> None:
    """kiro-cli colourises stdout; those bytes must not land in the
    reasoning or block the JSON parse."""
    raw = '\x1b[38;5;141m> \x1b[0m{"passed": true, "score": 0.8, "reasoning": "fine"}'
    v = parse_verdict(raw)
    assert v.passed is True
    assert v.score == 0.8
    assert "\x1b" not in v.reasoning


def test_parse_json_with_brace_in_reason() -> None:
    """A `}` inside a reason string must not truncate the object."""
    v = parse_verdict('{"passed": true, "score": 1.0, "reasoning": "saw a } brace"}')
    assert v.passed is True
    assert "brace" in v.reasoning


def test_parse_clean_json() -> None:
    v = parse_verdict('{"passed": true, "score": 0.9, "reasoning": "grounded"}')
    assert v.judged and v.passed and v.score == 0.9
    assert v.reasoning == "grounded"


def test_parse_json_with_fences_and_prose() -> None:
    raw = 'Sure!\n```json\n{"passed": false, "score": 0.2, "reasoning": "off-topic"}\n```\n'
    v = parse_verdict(raw)
    assert v.judged and not v.passed and v.score == 0.2


def test_parse_lenient_pass_fail() -> None:
    assert parse_verdict("Verdict: PASS, looks good").passed is True
    assert parse_verdict("FAIL — hallucinated a source").passed is False


def test_parse_unparseable_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_verdict("I'm not sure how to grade this honestly")


# --------------------------------------------------------------- CliJudge


async def test_clijudge_parses_runner_output() -> None:
    judge = CliJudge(["kiro-cli", "--headless"], runner=_runner('{"passed": true, "score": 1.0}'))
    v = await judge(_ji())
    assert v.judged and v.passed
    # The prompt was piped to the command on stdin.
    assert "Rubric" in judge._runner.seen[1]  # type: ignore[attr-defined]


async def test_clijudge_runner_error_is_unjudged() -> None:
    judge = CliJudge(["kiro-cli"], runner=_raising_runner(RuntimeError("boom")))
    v = await judge(_ji())
    assert v.judged is False
    assert "cli judge failed" in v.reasoning


async def test_clijudge_garbage_output_is_unjudged() -> None:
    judge = CliJudge(["kiro-cli"], runner=_runner("no verdict here"))
    v = await judge(_ji())
    assert v.judged is False
    assert "parse failed" in v.reasoning


def test_empty_command_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        CliJudge([])

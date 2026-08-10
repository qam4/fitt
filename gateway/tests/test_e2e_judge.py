"""Judged-e2e harness — CliJudge tests (Phase B). Fake runner, no kiro-cli."""

from __future__ import annotations

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


# --------------------------------------------------------------- parse


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

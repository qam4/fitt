"""Judged end-to-end harness — the frontier judge (Phase B).

A :data:`~gateway.e2e_eval.JudgeFn` backed by a headless CLI (kiro-cli
by default): build a rubric prompt from the :class:`JudgeInput`, run the
operator-configured command feeding the prompt on stdin, read the
verdict on stdout, and parse it. Modeled on the chess-coach
``CliProvider`` contract (`--judge-command`, `stdin -> stdout`,
temperature 0 set in the command's own flags).

Failure-isolated by contract (Property 3): a subprocess error, timeout,
or unparseable output yields an *un-judged* verdict — never an
exception into the run. The command is injected (a fake in tests), so
this module is fully unit-testable without kiro-cli.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable

from .e2e_eval import JudgeInput, JudgeVerdict

# (argv, stdin_text) -> stdout_text. Injected so tests don't shell out.
JudgeRunner = Callable[[list[str], str], Awaitable[str]]

_JUDGE_INSTRUCTIONS = (
    "You are grading an AI assistant's reply against a rubric. Reply with "
    'ONLY a JSON object: {"passed": true|false, "score": 0.0-1.0, '
    '"reasoning": "<one or two sentences>"}. Judge the reply quality per '
    "the rubric; the objective outcome below is provided as context "
    "(it was checked deterministically, not by you)."
)


def build_judge_prompt(ji: JudgeInput) -> str:
    """Compose the judge prompt from the scenario's rubric + the run."""
    tools = ", ".join(ji.tool_sequence) or "(none)"
    outcome = "PASS" if ji.outcome_passed else "FAIL"
    return (
        f"{_JUDGE_INSTRUCTIONS}\n\n"
        f"## Task\n{ji.intent}\n\n"
        f"## Rubric\n{ji.rubric}\n\n"
        f"## Objective outcome (deterministic)\n{outcome} — {ji.outcome_reason}\n\n"
        f"## Tools the assistant called\n{tools}\n\n"
        f"## Assistant reply\n{ji.reply}\n\n"
        "## Your verdict (JSON only)\n"
    )


def parse_verdict(raw: str) -> JudgeVerdict:
    """Parse a judge's stdout into a verdict.

    Prefers a JSON object ``{passed, score, reasoning}`` (tolerating
    surrounding prose / code fences by extracting the first {...} block);
    falls back to a PASS/FAIL keyword scan. Raises when nothing
    parseable is found (the caller turns that into an un-judged
    verdict)."""
    text = raw.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "passed" in obj:
            score = obj.get("score")
            return JudgeVerdict(
                passed=bool(obj["passed"]),
                score=float(score) if isinstance(score, int | float) else None,
                reasoning=str(obj.get("reasoning", "")),
            )
    # Lenient fallback: a bare PASS/FAIL somewhere in the output.
    upper = text.upper()
    if "PASS" in upper and "FAIL" not in upper:
        return JudgeVerdict(passed=True, score=None, reasoning=text[:200])
    if "FAIL" in upper and "PASS" not in upper:
        return JudgeVerdict(passed=False, score=None, reasoning=text[:200])
    raise ValueError(f"no parseable verdict in judge output: {text[:120]!r}")


async def _default_runner(argv: list[str], stdin_text: str) -> str:
    """Run ``argv`` feeding ``stdin_text``, return stdout (live path)."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin_text.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(
            f"judge command exited {proc.returncode}: {err.decode('utf-8', 'replace')[:200]}"
        )
    return out.decode("utf-8", "replace")


class CliJudge:
    """A :data:`JudgeFn` backed by a headless CLI command.

    ``command`` is the operator-configured argv (e.g. the kiro-cli
    invocation); ``runner`` is injected for testing. Callable as
    ``await judge(judge_input)``."""

    def __init__(
        self,
        command: list[str],
        *,
        runner: JudgeRunner | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        if not command:
            raise ValueError("judge command must be non-empty")
        self._command = command
        self._runner = runner or _default_runner
        self._timeout_s = timeout_s

    async def __call__(self, ji: JudgeInput) -> JudgeVerdict:
        prompt = build_judge_prompt(ji)
        try:
            raw = await asyncio.wait_for(self._runner(self._command, prompt), self._timeout_s)
        except Exception as exc:
            return JudgeVerdict.unjudged(f"cli judge failed: {type(exc).__name__}: {exc}")
        try:
            return parse_verdict(raw)
        except Exception as exc:
            return JudgeVerdict.unjudged(f"verdict parse failed: {exc}")

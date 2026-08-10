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
    '"reasoning": "<one or two sentences>"}.\n\n'
    "You are given the SYSTEM INTERNALS — the tools that actually executed "
    "and the resulting side effects (cron jobs, todo list, recent events), "
    "captured from the gateway's logs. TREAT THESE AS GROUND TRUTH about "
    "what really happened, over whatever the reply claims. A reply that "
    "claims it did something the internals don't support (e.g. 'I set a "
    "reminder' with no cron created, or fabricated search results with no "
    "web_search executed) should score LOW even if it reads well. The "
    "deterministic objective outcome is also provided; it was checked by "
    "code, not by you."
)

_MAX_FIELD = 1200


def _truncate(s: str, n: int = _MAX_FIELD) -> str:
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…(truncated)"


def _render_tool_calls(ji: JudgeInput) -> str:
    """Render each tool call with its args + result (Tier 1). Falls back
    to the bare name:status sequence when the structured detail isn't
    available (e.g. fake-dispatch tests)."""
    if not ji.tool_calls:
        return "Tools actually executed (in order): " + (
            ", ".join(ji.tool_sequence) or "(no tools executed)"
        )
    lines = ["Tools actually executed (in order, with args + result):"]
    for c in ji.tool_calls:
        status = "ok" if c.get("ok", True) else "ERROR"
        args = _truncate(json.dumps(c.get("args", {}), ensure_ascii=False, default=str), 400)
        result = _truncate(str(c.get("result", "")), 400)
        lines.append(f"  - {c.get('name', '?')}({args}) -> {status}: {result or '(no result)'}")
    return "\n".join(lines)


def _render_internals(ji: JudgeInput) -> str:
    """Render the tools + side-effect snapshot as the ground-truth block."""
    lines = [_render_tool_calls(ji)]
    if ji.loop_status and ji.loop_status != "ok":
        lines.append(f"Loop status: {ji.loop_status}" + (f" — {ji.error}" if ji.error else ""))
    snap = ji.snapshot or {}

    crons = snap.get("cron_jobs")
    if isinstance(crons, list):
        if crons:
            rendered = "; ".join(
                f"{c.get('name', '?')} [{c.get('schedule_kind', '?')}]"
                f" msg={c.get('message', '')!r} enabled={c.get('enabled', '?')}"
                for c in crons[:10]
            )
            lines.append(f"Cron jobs ({len(crons)}): {rendered}")
        else:
            lines.append("Cron jobs: none")

    if "todos_text" in snap:
        lines.append(f"todos.md:\n{_truncate(str(snap['todos_text'])) or '(empty)'}")

    kinds = snap.get("event_kinds")
    if isinstance(kinds, list) and kinds:
        lines.append(f"Recent event kinds: {', '.join(str(k) for k in kinds[-20:])}")

    return "\n".join(lines)


def _render_timeline(ji: JudgeInput) -> str:
    """Render the per-iteration turn timeline (Tier 2)."""
    lines = [
        "## Turn timeline (per-iteration trace of the agent loop)",
        "",
        "Each LLM call and tool call in order. Use this to diagnose the "
        "loop's BEHAVIOUR — e.g. the same tool re-emitted every iteration "
        "(a spiral), a tool that errored and was retried, huge out_tokens "
        "(the model reasoning instead of acting), or a finish_reason that "
        "explains an early stop.",
        "",
    ]
    for i, e in enumerate(ji.timeline, start=1):
        kind = e.get("kind", "?")
        if kind == "llm_request" and "messages" in e:
            # Verbatim JSON: this is the conversation as SENT, and the
            # exact wire shape (not a summary of it) is the evidence.
            body = json.dumps(e["messages"], ensure_ascii=False, default=str)
            lines.append(
                f"{i:>3}. llm_request  iteration={e.get('iteration')}  "
                f"messages_sent={_truncate(body, 2000)}"
            )
            continue
        bits: list[str] = []
        for key in (
            "iteration",
            "tool_name",
            "ok",
            "in_tokens",
            "out_tokens",
            "finish_reason",
            "tool_calls_count",
            "decision",
        ):
            if key in e:
                bits.append(f"{key}={e[key]}")
        if "args" in e:
            bits.append(
                "args=" + _truncate(json.dumps(e["args"], ensure_ascii=False, default=str), 200)
            )
        if "result_summary" in e:
            bits.append("result=" + _truncate(str(e["result_summary"]), 200))
        lines.append(f"{i:>3}. {kind}  " + "  ".join(bits))
    return "\n".join(lines)


_DIAGNOSE_ASK = (
    "In addition to the verdict, the ``reasoning`` field MUST state your "
    "best hypothesis for the ROOT CAUSE of any failure, citing the "
    "timeline (which iteration, which tool, what changed between "
    "iterations). Be specific and mechanical, not generic."
)


def build_judge_prompt(ji: JudgeInput) -> str:
    """Compose the judge prompt: rubric + the reply + the system internals
    (ground truth) the judge grades against.

    When ``ji.timeline`` is populated (Tier 2) the per-iteration trace is
    appended and the judge is additionally asked to diagnose the root
    cause — that's what turns the harness from "it failed" into "it
    failed because iteration N re-emitted the same call"."""
    outcome = "PASS" if ji.outcome_passed else "FAIL"
    prompt = (
        f"{_JUDGE_INSTRUCTIONS}\n"
        + (f"\n{_DIAGNOSE_ASK}\n" if ji.timeline else "")
        + f"\n## Task\n{ji.intent}\n\n"
        f"## Rubric\n{ji.rubric}\n\n"
        f"## System internals (GROUND TRUTH — what actually happened)\n"
        f"{_render_internals(ji)}\n\n"
        f"## Objective outcome (deterministic, checked by code)\n"
        f"{outcome} — {ji.outcome_reason}\n\n"
        f"## Assistant reply to the user\n{_truncate(ji.reply)}\n\n"
    )
    if ji.timeline:
        prompt += f"{_render_timeline(ji)}\n\n"
    return prompt + "## Your verdict (JSON only)\n"


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """Remove ANSI colour/escape sequences.

    A chatty CLI judge (kiro-cli) colourises its output; those bytes end
    up inside the stored ``reasoning`` and can also sit between the
    prompt marker and the JSON. Strip them before parsing."""
    return _ANSI_RE.sub("", text)


def _extract_json_object(text: str) -> str | None:
    """Return the first *complete* brace-matched {...} object, or None.

    Brace-matches (respecting string literals + escapes) instead of a
    greedy ``\\{.*\\}`` regex so a truncated tail doesn't produce a
    half-object that fails to parse, and a ``}`` inside a reason string
    doesn't cut the object short."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_verdict(raw: str) -> JudgeVerdict:
    """Parse a judge's stdout into a verdict.

    Prefers a JSON object ``{passed, score, reasoning}`` (tolerating
    surrounding prose, code fences, and ANSI colour codes).

    **A detected-but-unparseable JSON verdict raises** rather than
    falling through to the keyword scan. That fallback inverted a real
    verdict once (2026-08-10): a truncated reply whose JSON said
    ``"passed": false`` was scored PASS because the prose contained the
    word "PASS" ("the objective outcome is PASS"). A false pass is the
    worst failure mode for an eval, so when the judge clearly *tried* to
    emit JSON we refuse to guess — the caller records it un-judged. The
    keyword scan survives only for judges that never emit JSON at all."""
    text = strip_ansi(raw).strip()
    candidate = _extract_json_object(text)
    if candidate is not None:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"judge emitted JSON that failed to parse (refusing to guess "
                f"from prose): {exc}: {candidate[:120]!r}"
            ) from exc
        if isinstance(obj, dict) and "passed" in obj:
            score = obj.get("score")
            return JudgeVerdict(
                passed=bool(obj["passed"]),
                score=float(score) if isinstance(score, int | float) else None,
                reasoning=str(obj.get("reasoning", "")),
            )
        raise ValueError(f"judge JSON has no 'passed' field: {candidate[:120]!r}")
    if "{" in text:
        # An opening brace with no complete object == truncated JSON.
        raise ValueError(f"judge output has truncated JSON: {text[-120:]!r}")
    # Lenient fallback, only for output with no JSON attempt at all.
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

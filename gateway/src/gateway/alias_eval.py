"""Alias eval harness (hallucinations doc item 6).

Why this exists
---------------

The boot-time probe (:mod:`gateway.alias_probe`) is a single
canary: one request, one synthetic tool, one pass/fail signal.
Good for "did the binding survive the swap," not good enough
for "does this alias handle the workload we actually ask of
it."

This module is the rung above the probe. It runs a curated
set of :class:`EvalCase` s against one alias, each with a
prompt and an expected-tool-call shape, and produces a
pass/fail report the operator can read without digging
through raw logs. Same shape-level classification as the
probe (real ``tool_calls`` structure is the signal, not
content regex); richer coverage because each case targets a
specific capability.

The eval harness pays off in two situations:

* **Before a model swap lands.** Run the current suite against
  the new binding on a scratch config before committing it in
  ``config.yaml``. If pass rate drops below the historical
  baseline on the same prompts, don't swap yet.
* **After a model swap.** Scheduled (or manual) re-run on the
  bound alias. If behaviour drifts — providers update weights
  behind the API, serving stacks change — the pass rate
  falls and the operator notices before a live session does.

Deliberately NOT in scope
-------------------------

* **Not a benchmark.** No comparison against BFCL / HELM /
  SWE-bench. Those exist; we're measuring "does this alias
  still work for FITT's workload," not "is this model good
  in the abstract."
* **Not a fuzzer.** Case count stays small (4-6 to start,
  maybe 12-20 long-term). Each case exercises one distinct
  tool-use pattern. A broad random suite would dilute the
  signal and cost real tokens.
* **Not a performance test.** We record latency per case for
  the report, but thresholds on latency are user-tunable and
  not on the pass/fail path.
* **Not multi-alias in one invocation.** Each
  :func:`run_eval_suite` call is one alias. The CLI can loop
  for a multi-alias run.

Where the cases live
--------------------

In code. Two reasons:

1. The canonical set evolves tightly with FITT's tool
   registry. Keeping the cases next to the tool definitions
   means a new tool naturally prompts a new case.
2. A YAML schema for "the expected tool call" is a thing we'd
   have to maintain. Python is already the schema. When the
   set grows past ~20 cases, we split into a
   ``$FITT_HOME/eval/cases.yaml`` + loader; not before.

Output
------

A single markdown report at
``$FITT_HOME/eval/<alias>-<timestamp>.md`` plus a rolling
``$FITT_HOME/eval/alias-report.md`` that holds the latest run
per alias (one section per alias). The rolling file is what
``fitt eval alias fitt-smart`` overwrites per call; the
timestamped file is the audit trail.

The report is intentionally human-first: "4/5 passed, here's
what failed, here's the narrated reply." Operators read it
before making a decision. Machine consumers that want
structured data should call :func:`run_eval_suite` directly
and inspect the :class:`EvalReport`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal

from .agent_loop import (
    assistant_message_from_response,
    extract_tool_calls,
    response_to_dict,
)

if TYPE_CHECKING:
    from .router import AliasRouter

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- case


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One curated test against an alias.

    * ``name``: slug for the report ("read_file_basic",
      "no_tool_small_talk"). Lower-snake-case so filenames and
      grep patterns are easy.
    * ``prompt``: the user message the alias sees. Phrased
      to trigger the expected tool when one is expected, or
      the "no tool" answer when none is expected.
    * ``tools``: tool schemas offered in the ``tools`` array.
      Minimal (1-3 per case) — we don't want cross-tool
      disambiguation to dominate the signal.
    * ``expected_tool``: name of the tool we expect the model
      to call. ``None`` for "model should answer without a
      tool" cases (the small-talk / clearly-out-of-scope
      variety).
    * ``description``: a sentence for the report readers so
      they know what pattern this case is probing.
    """

    name: str
    prompt: str
    tools: list[dict[str, Any]]
    expected_tool: str | None
    description: str = ""


# --------------------------------------------------------------- result


CaseStatus = Literal[
    "pass",
    "wrong_tool",
    "narrated",
    "no_tool_expected_but_called",
    "truncated",
    "transport_error",
]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Outcome of running one :class:`EvalCase`.

    ``status``:

    * ``pass``: the expected shape was emitted (right tool, or
      no tool when none was expected with a short reply).
    * ``wrong_tool``: a tool was called, but not the expected
      one. Name recorded in ``tool_called``.
    * ``narrated``: tools were offered, model chose not to call,
      reply was long enough to be narration (same threshold
      as the probe).
    * ``no_tool_expected_but_called``: we asked for small talk,
      the model reached for a tool anyway. Useful signal when
      an alias is over-eager.
    * ``truncated``: response hit ``finish_reason=length``.
    * ``transport_error``: dispatch raised or timed out.
    """

    case_name: str
    status: CaseStatus
    detail: str
    latency_ms: int
    tool_called: str | None = None
    finish_reason: str | None = None
    reply_preview: str = ""


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate outcome of a suite run against one alias."""

    alias: str
    model_id: str | None
    started_at: datetime
    finished_at: datetime
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.status == "pass")

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        """Float in [0, 1]. Zero cases → 0.0 so downstream
        callers that branch on ``pass_rate < threshold`` don't
        false-positive when the suite is empty."""
        return self.passed / self.total if self.total else 0.0


# --------------------------------------------------------------- default cases


def default_cases() -> list[EvalCase]:
    """Return the canonical starter suite.

    Small by design (see module docstring). Each case targets
    a distinct pattern. Ordering is intentional: read first,
    then search, then negative case (small talk), then
    list-capabilities (meta)."""
    read_file_tool = {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from a registered project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["project", "path"],
            },
        },
    }
    grep_tool = {
        "type": "function",
        "function": {
            "name": "grep_repo",
            "description": "Grep across a registered project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "pattern": {"type": "string"},
                },
                "required": ["project", "pattern"],
            },
        },
    }
    list_caps_tool = {
        "type": "function",
        "function": {
            "name": "list_capabilities",
            "description": "List the tools FITT exposes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    }
    return [
        EvalCase(
            name="read_file_basic",
            prompt=(
                "Read the first few lines of README.md in the `hub` "
                "project. Use the read_file tool; don't narrate."
            ),
            tools=[read_file_tool],
            expected_tool="read_file",
            description=(
                "Baseline single-tool call. If this fails the "
                "binding has a fundamental tool-call problem."
            ),
        ),
        EvalCase(
            name="grep_repo_basic",
            prompt=("Search the `hub` project for occurrences of 'TODO'. Use the grep_repo tool."),
            tools=[grep_tool],
            expected_tool="grep_repo",
            description=(
                "Different tool, different arg shape. Catches bindings that overfit to one tool."
            ),
        ),
        EvalCase(
            name="tool_disambiguation",
            prompt=(
                "Open README.md in the `hub` project and show me "
                "its contents. You have two tools available; pick "
                "the right one."
            ),
            tools=[read_file_tool, grep_tool],
            expected_tool="read_file",
            description=(
                "Two tools offered, one correct answer. Catches "
                "bindings that pick the first tool by default."
            ),
        ),
        EvalCase(
            name="no_tool_small_talk",
            prompt="What is 2 + 2?",
            tools=[read_file_tool, grep_tool],
            expected_tool=None,
            description=(
                "Tools offered but irrelevant. A good binding "
                "answers inline with no tool call. Catches over-"
                "eager tool-callers."
            ),
        ),
        EvalCase(
            name="list_capabilities_meta",
            prompt=("List the tools you have access to. Use the list_capabilities tool."),
            tools=[list_caps_tool],
            expected_tool="list_capabilities",
            description=(
                "Meta tool call with no arguments. Catches "
                "bindings that only tool-call when args are "
                "required."
            ),
        ),
    ]


# --------------------------------------------------------------- runner


async def run_eval_case(
    case: EvalCase,
    alias: str,
    router: AliasRouter,
    *,
    timeout_s: float = 15.0,
) -> CaseResult:
    """Dispatch one :class:`EvalCase` against ``alias`` and
    classify the response.

    Never raises; transport failures become ``transport_error``
    results. This matches the probe's contract so operators
    running a 20-case suite get a full report even when one
    alias is unreachable."""
    request_body: dict[str, Any] = {
        "messages": [{"role": "user", "content": case.prompt}],
        "tools": case.tools,
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": 512,
    }
    started = perf_counter()
    try:
        dispatch = await asyncio.wait_for(router.dispatch(alias, request_body), timeout=timeout_s)
    except TimeoutError:
        return CaseResult(
            case_name=case.name,
            status="transport_error",
            detail=f"timed out after {int(timeout_s)}s",
            latency_ms=int((perf_counter() - started) * 1000),
        )
    except Exception as exc:
        return CaseResult(
            case_name=case.name,
            status="transport_error",
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=int((perf_counter() - started) * 1000),
        )

    latency_ms = int((perf_counter() - started) * 1000)
    response = dispatch.response
    tool_calls = extract_tool_calls(response)

    # Pull finish_reason + reply for narration / length
    # classification; same shape as alias_probe.
    finish_reason: str | None = None
    reply = ""
    dumped = response_to_dict(response)
    if dumped:
        choices = dumped.get("choices")
        if isinstance(choices, list) and choices:
            choice0 = choices[0]
            if isinstance(choice0, dict):
                fr = choice0.get("finish_reason")
                if isinstance(fr, str):
                    finish_reason = fr
    msg = assistant_message_from_response(response)
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            reply = content

    # --- classification -------------------------------------------

    if case.expected_tool is None:
        # Negative case: we don't want a tool call.
        if tool_calls:
            name = tool_calls[0].get("function", {}).get("name") or "(unknown)"
            return CaseResult(
                case_name=case.name,
                status="no_tool_expected_but_called",
                detail=f"model called {name!r} when no tool was expected",
                latency_ms=latency_ms,
                tool_called=name,
                finish_reason=finish_reason,
            )
        # Reply is the correct answer shape. We don't grade
        # the content — "2 + 2 = 4" vs "it's 4" is not what
        # we're measuring. Long prose is fine here; the goal
        # is that the model didn't reach for a tool.
        return CaseResult(
            case_name=case.name,
            status="pass",
            detail="no tool call as expected",
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            reply_preview=_preview(reply),
        )

    # Positive case: we DO want a specific tool.
    if tool_calls:
        name = tool_calls[0].get("function", {}).get("name") or ""
        if name == case.expected_tool:
            return CaseResult(
                case_name=case.name,
                status="pass",
                detail=f"called {name!r} as expected",
                latency_ms=latency_ms,
                tool_called=name,
                finish_reason=finish_reason,
            )
        return CaseResult(
            case_name=case.name,
            status="wrong_tool",
            detail=(f"expected {case.expected_tool!r}, got {name!r}"),
            latency_ms=latency_ms,
            tool_called=name,
            finish_reason=finish_reason,
        )

    # No tool call on a positive case.
    if finish_reason == "length":
        return CaseResult(
            case_name=case.name,
            status="truncated",
            detail="model hit max_tokens before emitting tool_calls",
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            reply_preview=_preview(reply),
        )
    # Narration threshold matches the probe.
    if len(reply) >= 30:
        return CaseResult(
            case_name=case.name,
            status="narrated",
            detail=(f"model replied with {len(reply)} chars instead of emitting tool_calls"),
            latency_ms=latency_ms,
            finish_reason=finish_reason,
            reply_preview=_preview(reply),
        )
    # Empty-ish reply + no tool call: transport-class anomaly.
    return CaseResult(
        case_name=case.name,
        status="transport_error",
        detail="empty reply and no tool_calls",
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )


async def run_eval_suite(
    alias: str,
    router: AliasRouter,
    *,
    cases: list[EvalCase] | None = None,
    timeout_s: float = 15.0,
) -> EvalReport:
    """Run every case in ``cases`` (or the default suite) against
    ``alias``, sequentially.

    Sequential rather than concurrent: most aliases share a
    single backend quota (NIM free tier, OpenRouter's rate
    limits), and a 5-case parallel burst is a good way to
    get rate-limited through no fault of the case design.
    A 5-case sequential run takes roughly 15-25 seconds;
    acceptable for an on-demand operator command."""
    cases = cases if cases is not None else default_cases()
    started_at = datetime.now(UTC)
    results: list[CaseResult] = []
    model_id: str | None = None

    for case in cases:
        r = await run_eval_case(case, alias, router, timeout_s=timeout_s)
        results.append(r)
        # Best-effort: capture the model id from the first
        # successful dispatch. We don't re-resolve per case
        # because AliasRouter already handles fallback per call.
        if model_id is None and r.status != "transport_error":
            try:
                primary = router.resolve(alias)[0]
                model_id = primary.id
            except Exception:
                pass

    finished_at = datetime.now(UTC)
    return EvalReport(
        alias=alias,
        model_id=model_id,
        started_at=started_at,
        finished_at=finished_at,
        cases=results,
    )


# --------------------------------------------------------------- report


def render_report_markdown(report: EvalReport) -> str:
    """Render an :class:`EvalReport` as a human-first markdown
    string suitable for writing to disk or piping to a pager.

    Format: header with alias + model + timing + pass rate, then
    one section per case with status, latency, and — for
    failures — the preview or ``tool_called`` or ``finish_reason``
    that explains why."""
    lines: list[str] = []
    lines.append(f"# Eval report — `{report.alias}`")
    lines.append("")
    lines.append(f"- Model: `{report.model_id or '(unknown)'}`")
    lines.append(f"- Started: {report.started_at.isoformat()}")
    lines.append(f"- Finished: {report.finished_at.isoformat()}")
    duration_ms = int((report.finished_at - report.started_at).total_seconds() * 1000)
    lines.append(f"- Duration: {duration_ms} ms")
    lines.append(
        f"- Result: **{report.passed}/{report.total} passed** ({report.pass_rate * 100:.0f}%)"
    )
    lines.append("")
    lines.append("## Cases")
    lines.append("")

    for c in report.cases:
        icon = "✅" if c.status == "pass" else "❌"
        lines.append(f"### {icon} `{c.case_name}` — {c.status}")
        lines.append("")
        lines.append(f"- Latency: {c.latency_ms} ms")
        if c.tool_called is not None:
            lines.append(f"- Tool called: `{c.tool_called}`")
        if c.finish_reason is not None:
            lines.append(f"- Finish reason: `{c.finish_reason}`")
        lines.append(f"- Detail: {c.detail}")
        if c.reply_preview:
            lines.append("- Reply preview:")
            lines.append("")
            lines.append("  ```")
            lines.append(f"  {c.reply_preview}")
            lines.append("  ```")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------- paths


def default_eval_dir(fitt_home: Path) -> Path:
    """Return the canonical eval output directory. Created on
    first write by :func:`write_report`."""
    return fitt_home / "eval"


def write_report(
    report: EvalReport, fitt_home: Path, *, suite: str = "default"
) -> tuple[Path, Path]:
    """Persist ``report`` to both the timestamped audit-trail
    file and the rolling per-alias latest-report file.

    ``suite`` namespaces the file paths so the default suite
    and the coding-agent suite (and any future per-workload
    suite) don't overwrite each other:

    * ``suite="default"`` → ``<alias>-latest.md`` and
      ``<alias>-<timestamp>.md`` (the existing paths,
      preserved so existing operator-saved reports keep
      their names).
    * ``suite="coding"`` → ``<alias>-coding-latest.md``
      and ``<alias>-coding-<timestamp>.md``.
    * Any other suite name slugs into ``<alias>-<suite>-...``
      with a forward-compatible naming.

    Returns the ``(timestamped_path, rolling_path)`` pair so
    the CLI can tell the operator exactly where the output
    landed."""
    eval_dir = default_eval_dir(fitt_home)
    eval_dir.mkdir(parents=True, exist_ok=True)
    body = render_report_markdown(report)
    ts = report.finished_at.strftime("%Y-%m-%dT%H-%M-%S")
    suffix = "" if suite == "default" else f"-{suite}"
    timestamped = eval_dir / f"{report.alias}{suffix}-{ts}.md"
    rolling = eval_dir / f"{report.alias}{suffix}-latest.md"
    timestamped.write_text(body, encoding="utf-8")
    rolling.write_text(body, encoding="utf-8")
    return timestamped, rolling


# --------------------------------------------------------------- helpers


def _preview(text: str, *, cap: int = 200) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= cap:
        return collapsed
    return collapsed[: cap - 5] + "[...]"

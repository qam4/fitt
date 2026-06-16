"""Named multi-step scenarios for the planning/execution evals (Phase 12).

Why this exists
---------------

Phase 12's running example is a *multi-step* turn — fetch live
information, then synthesize and deliver it — that the flat agent
loop handles poorly and elected planning is meant to fix. Three
tasks reference the same ``daily_news_summary`` case:

* **Task 4** — run the *flat* loop on it against a real model and
  read the actual failure (the documented baseline).
* **Task 22** — run it through both the flat loop and the
  orchestrated (planned) loop and compare pass rates.
* **Task 21** — a cron firing of it with compaction mid-run still
  delivers.

Defining the case once, here, keeps those three measurements
apples-to-apples instead of each re-deriving a prompt.

Scenario vs EvalCase
--------------------

An :class:`~gateway.alias_eval.EvalCase` is *single-shot*: one
dispatch, one tool-call-shape check. A :class:`Scenario` is a
*whole turn* run through the agent loop (flat) or the orchestrator
(planned). The signal is the structural shape of the resulting
:class:`~gateway.agent_loop.AgentLoopResult` — which tools
actually ran (from ``tool_calls_for_memory``) and whether the task
was completed — never the exact words of the reply. That's
convention 1 from the Phase 12 task-2 conventions (assert on
structure, never strings).

:func:`classify_news_outcome` folds a result into one of a small
set of :data:`ScenarioOutcome` labels so a multi-sample run
(convention 2) can compute a pass rate. One label
(``upstream_error``) is a transient infra failure and is excluded
from a capability denominator (convention 3), mirroring
:data:`gateway.alias_eval.DISPATCH_FAILURE_STATUSES`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .agent_loop import AgentLoopResult


ScenarioOutcome = Literal[
    # The task was completed: the model fetched live information and
    # then delivered a substantive answer (either via send_message or
    # a grounded inline reply). This is "pass".
    "completed",
    # Fetched, but never delivered — searched and then stalled, or
    # produced only a thin reply. The classic "got the data, didn't
    # finish the job" multi-step failure.
    "searched_not_delivered",
    # Never fetched at all: answered from training data, refused as
    # "I can't access real-time info", or narrated instead of acting.
    # The headline flat-loop failure the planner is meant to fix.
    "no_search",
    # A web_search was attempted but errored (and none succeeded).
    "tool_error",
    # Hit the iteration budget without completing.
    "exhausted",
    # The dispatch itself failed (transport / 5xx). Transient infra,
    # not a capability miss — excluded from the pass-rate denominator.
    "upstream_error",
]


# Outcomes that mean the turn never got a fair chance (transient
# infra failure), to exclude from a capability pass-rate denominator.
# Mirrors :data:`gateway.alias_eval.DISPATCH_FAILURE_STATUSES`.
TRANSIENT_OUTCOMES: frozenset[str] = frozenset({"upstream_error"})


# Minimum substantive-reply length (chars) for an inline answer to
# count as "delivered" when no send_message was called. Same spirit
# as the narration threshold in alias_eval / alias_probe, scaled up:
# a real news summary is several bullet points, not a one-liner.
_SUBSTANTIVE_REPLY_CHARS = 200


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named multi-step turn for the planning/execution evals.

    * ``user_message``: the turn's user prompt. Phrased to need a
      live fetch *and* a synthesis/delivery step — genuinely
      multi-step, so the flat loop has a real chance to stumble.
    * ``min_searches``: how many successful ``web_search`` calls the
      classifier requires before it considers the fetch step done.
      One is enough for the canonical case; raise it for cases that
      should fan out across queries.
    """

    name: str
    user_message: str
    description: str = ""
    min_searches: int = 1


def daily_news_summary() -> Scenario:
    """The canonical multi-step case: fetch today's news, then
    summarize and (optionally) push it.

    Uses FITT's *real* registered tools — ``web_search`` to fetch,
    ``send_message`` to deliver — rather than synthetic tool schemas.
    The turn is run against the live tool registry so task 4 reads a
    faithful failure, not a contrived one."""
    return Scenario(
        name="daily_news_summary",
        user_message=(
            "Search the web for today's top news headlines, then give me a short "
            "summary as three or four bullet points. If you can push it to me as a "
            "message, do that too."
        ),
        description=(
            "Two-step turn: fetch live information (web_search), then synthesize and "
            "deliver it (a grounded summary, optionally via send_message). The flat "
            "loop tends to either skip the fetch (answer from stale training data / "
            "refuse) or fetch-then-stall; elected planning is meant to carry it "
            "through both steps."
        ),
    )


def _ok_calls(result: AgentLoopResult, tool_name: str) -> int:
    """Count successful calls to ``tool_name`` in the turn."""
    return sum(
        1
        for c in result.tool_calls_for_memory
        if c.tool_name == tool_name and c.result_status == "ok"
    )


def _errored_calls(result: AgentLoopResult, tool_name: str) -> int:
    """Count failed calls to ``tool_name`` in the turn."""
    return sum(
        1
        for c in result.tool_calls_for_memory
        if c.tool_name == tool_name and c.result_status != "ok"
    )


def classify_news_outcome(
    result: AgentLoopResult,
    scenario: Scenario | None = None,
) -> ScenarioOutcome:
    """Classify a finished turn against the news scenario, structurally.

    Reads only facts the :class:`~gateway.agent_loop.AgentLoopResult`
    already carries — the executed tool calls and their statuses, the
    loop status, and the final reply length. No prose-shape or intent
    inference (the same C4 discipline the trouble detector follows).

    Precedence (most-specific first):

    1. ``upstream_error`` — the dispatch failed; nothing else is
       knowable.
    2. fetch step:
       * a successful ``web_search`` (>= ``min_searches``) → proceed to
         the delivery check;
       * else a ``web_search`` that errored → ``tool_error``;
       * else budget hit → ``exhausted``;
       * else → ``no_search``.
    3. delivery step (only reached when the fetch succeeded):
       * a successful ``send_message`` OR a substantive inline reply →
         ``completed``;
       * else budget hit → ``exhausted``;
       * else → ``searched_not_delivered``.
    """
    sc = scenario or daily_news_summary()

    if result.status == "upstream_error":
        return "upstream_error"

    successful_searches = _ok_calls(result, "web_search")
    fetched = successful_searches >= sc.min_searches

    if not fetched:
        if _errored_calls(result, "web_search") > 0:
            return "tool_error"
        if result.status == "tool_loop_exhausted":
            return "exhausted"
        return "no_search"

    delivered = (
        _ok_calls(result, "send_message") > 0
        or len(result.assistant_text.strip()) >= _SUBSTANTIVE_REPLY_CHARS
    )
    if delivered:
        return "completed"
    if result.status == "tool_loop_exhausted":
        return "exhausted"
    return "searched_not_delivered"

"""Phase 12 task 14 — recovery actions.

Consumes a :class:`gateway.trouble.Trouble` (task 13) and decides what
to do about it. The policy here is *pure* (no IO) so it is exhaustively
unit-testable; the orchestrator owns the side effects (re-running the
executor pass, rebuilding a clean context). The escalation ladder,
cheapest first (Story 4.1, design line "nudge -> repair -> re-plan ->
honest stop"):

1. ``nudge`` — a bounded retry: re-run the executor with the
   ``recover``-step prompt appended to the *existing* transcript. This
   is Hermes's "process the tool results above and continue" rung and
   also carries repair guidance ("if a step failed, retry it
   differently or report honestly"), so a malformed/failed call gets a
   chance to be repaired without throwing the context away.
2. ``replan`` — restart execution on a **clean context** (Story 5.3):
   the flailing transcript is discarded and only the goal plus the
   progress-bearing plan are carried forward. Used straight away for
   signals a nudge cannot help (zero progress, exhausted budget), and
   as the escalation when a nudge did not clear the trouble.
3. ``stop`` — honest stop (Story 4.2): end the turn with a truthful
   report naming what was observed and how far the plan got, never a
   fabricated result.

Invariants:
- **C6 / Story 4.3** — no rung rebinds the turn to a different (e.g.
  cloud) alias. Recovery re-runs the executor on the *same* alias it
  was already using. The only capability lever is the operator's
  ``planner_alias`` config, never an automatic in-turn escalation.
- Recovery is bounded: at most :data:`MAX_RECOVERY_ATTEMPTS` actions
  before an honest stop, and ``replan`` happens at most once.

A genuine capability gap ("I'd need a tool to X") is a *terminal
honest outcome*, distinct from thrash, and is task 15 — it is not
retried or escalated here.
"""

from __future__ import annotations

from typing import Literal

from .plan_store import Plan
from .trouble import Trouble

RecoveryAction = Literal["nudge", "replan", "stop"]
"""What the orchestrator should do about a detected trouble."""

MAX_RECOVERY_ATTEMPTS = 2
"""Upper bound on recovery actions per turn before an honest stop.
Two is enough for the intended ladder (nudge, then a clean re-plan);
a third persistent trouble means the turn isn't going to recover, so
we stop honestly rather than burn more budget."""


def decide_recovery(trouble: Trouble, *, attempt: int, replanned: bool) -> RecoveryAction:
    """Pick the next recovery action from observable state only.

    ``attempt`` is how many recovery actions have already run this turn
    (0 on the first trouble). ``replanned`` records whether a clean
    re-plan has already happened — a second one is pointless, so we
    stop instead.

    Decisions reference only the trouble *kind* and the bounded
    counters, never model prose (property C4 carries through from the
    detector).
    """
    if not trouble or attempt >= MAX_RECOVERY_ATTEMPTS:
        return "stop"

    # Signals a continue-nudge cannot fix: the turn already made many
    # failed attempts, or ran out of room. Go straight to a clean
    # re-plan once; if we already did, stop honestly.
    if trouble.kind in ("zero_progress", "budget_exhausted"):
        return "replan" if not replanned else "stop"

    # Cheaper signals (empty-after-tools, a tool error, an identical
    # failing retry): nudge first. If a nudge already ran and the
    # trouble recurred, escalate to a clean re-plan, then stop.
    if attempt == 0:
        return "nudge"
    if not replanned:
        return "replan"
    return "stop"


def honest_report(trouble: Trouble, plan: Plan | None) -> str:
    """Build the honest stop message (Story 4.2).

    Names what was observed (the trouble's fact-based detail) and how
    far the plan got, so the user can trust the report. Never
    fabricates a result.
    """
    parts = [
        "I couldn't complete this reliably, so I'm stopping instead of guessing.",
        f"What I observed: {trouble.detail}.",
    ]
    if plan is not None and plan.items:
        done = sum(1 for item in plan.items if item.status == "done")
        total = len(plan.items)
        nxt = plan.next_pending()
        progress = f"Progress: {done}/{total} planned steps completed."
        if nxt is not None:
            progress += f" The next incomplete step was: {nxt.text!r}."
        parts.append(progress)
    return " ".join(parts)


__all__ = ["MAX_RECOVERY_ATTEMPTS", "RecoveryAction", "decide_recovery", "honest_report"]

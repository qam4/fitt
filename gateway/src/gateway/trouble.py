"""Phase 12 task 13 — ground-truth trouble detector.

Classifies whether an executor pass ran into trouble, using only
**observable facts** about what happened: a tool returned an error,
the model re-issued an identical failing call, the final reply was
empty after tools ran, the whole turn made zero successful tool
calls, or the iteration budget was exhausted.

Property C4 (design.md): every classification references only
observable facts. It NEVER reads the shape or length of the model's
prose reply — "empty after tools" is the binary fact *did the model
produce any final content at all*, not an interpretation of what the
prose says. The ``claim_check`` / narration rollbacks recorded in
``docs/observed-issues.md`` are exactly why this line is drawn hard:
inferring intent or "this looks like flailing" from prose was a
repeated source of false positives.

This module only *detects*. Mapping a trouble kind to an escalating
recovery action (continue-nudge -> repair -> re-plan on a clean
context -> honest stop) is task 14; it consumes the ``Trouble`` this
returns.

Inputs are the facts a finished :class:`gateway.agent_loop.AgentLoopResult`
already carries — ``status``, ``tool_calls_for_memory`` (ordered
:class:`gateway.memory.PersistedToolCall` records), and
``assistant_text`` — so the orchestrator can call this straight off
the executor pass's result without re-deriving anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .memory import PersistedToolCall

TroubleKind = Literal[
    "none",
    "empty_after_tools",
    "identical_retry",
    "zero_progress",
    "tool_error",
    "budget_exhausted",
]
"""The ground-truth trouble signals (Story 5.1). ``none`` means the
transcript is clean — the turn terminated normally with a non-empty
reply and no observable failure."""

# The loop's exhausted-budget status discriminant (agent_loop sets
# this when it hits ``max_iterations`` without a natural stop).
_EXHAUSTED_STATUS = "tool_loop_exhausted"

# Default number of tool attempts with zero successes before the
# turn is judged to be making no progress (Story 5.1, "N iterations
# with zero successful tool calls"). Overridable per call.
_DEFAULT_ZERO_PROGRESS_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class Trouble:
    """The classification result.

    ``kind`` is the trouble discriminant (``"none"`` when clean).
    ``detail`` is a short, fact-based description for logging and the
    turn-event stream — it states *what was observed*, never an
    interpretation of the model's prose. Truthiness mirrors
    ``kind != "none"`` so callers can write ``if detect_trouble(...):``.
    """

    kind: TroubleKind
    detail: str = ""

    def __bool__(self) -> bool:
        return self.kind != "none"


NO_TROUBLE = Trouble(kind="none")


def _succeeded(call: PersistedToolCall) -> bool:
    """A tool call succeeded iff its persisted status is exactly
    ``"ok"``. Anything else — ``"error"`` or the shell convention
    ``"exit=N"`` — is a failure (see
    ``agent_loop._status_and_summary_from_result``)."""
    return call.result_status == "ok"


def detect_trouble(
    *,
    status: str,
    tool_calls: Sequence[PersistedToolCall],
    assistant_text: str,
    zero_progress_threshold: int = _DEFAULT_ZERO_PROGRESS_THRESHOLD,
) -> Trouble:
    """Classify the executor pass's outcome into a single
    :class:`Trouble`, using only observable facts (property C4).

    Precedence is most-specific / cheapest-recoverable first, so a
    concrete cause is reported in preference to the generic
    budget-exhaustion catch-all when both hold:

    1. ``empty_after_tools`` — natural stop, tools ran, the final
       reply has no content (Hermes's cheapest continue-nudge case).
    2. ``identical_retry`` — the last two calls are identical
       (same name + args) and the latest still failed: the classic
       doom loop, caught fast (before the zero-progress threshold).
    3. ``zero_progress`` — at least ``zero_progress_threshold`` tool
       attempts and not one succeeded.
    4. ``tool_error`` — the most recent tool call returned an error
       (fewer attempts than the zero-progress threshold, or some
       earlier call did succeed).
    5. ``budget_exhausted`` — the loop hit its iteration cap without
       any of the more specific facts above.

    ``status`` values other than ``"ok"`` / ``"tool_loop_exhausted"``
    (e.g. ``"upstream_error"``) are out of scope for recovery — the
    loop surfaces those through its own error path — and yield
    ``none`` here unless a tool-level fact independently fires.
    """
    calls = list(tool_calls)
    ran_tools = len(calls) > 0

    # 1. empty-after-tools — only on a *natural* stop. An exhausted
    #    turn also ends with no final content, but that is budget
    #    exhaustion, not this signal; gating on status keeps the two
    #    disjoint. Emptiness is presence-of-content, not prose shape.
    if status == "ok" and ran_tools and not assistant_text.strip():
        return Trouble(
            "empty_after_tools",
            f"final reply empty after {len(calls)} tool call(s)",
        )

    # 2. identical failing retry — last two calls identical and the
    #    latest one failed. (A retry that finally *succeeded* is
    #    recovery working, not trouble, so it must not fire.)
    if len(calls) >= 2:
        last, prev = calls[-1], calls[-2]
        if last.tool_name == prev.tool_name and last.args == prev.args and not _succeeded(last):
            return Trouble(
                "identical_retry",
                f"identical failing call to {last.tool_name!r} repeated",
            )

    # 3. zero progress — enough attempts, none succeeded. Checked
    #    before tool_error so a sustained failure run escalates
    #    rather than reporting the last error in isolation.
    if len(calls) >= zero_progress_threshold and not any(_succeeded(c) for c in calls):
        return Trouble(
            "zero_progress",
            f"{len(calls)} tool call(s), 0 succeeded",
        )

    # 4. tool error — most recent call errored.
    if ran_tools and not _succeeded(calls[-1]):
        return Trouble(
            "tool_error",
            f"{calls[-1].tool_name!r} returned {calls[-1].result_status!r}",
        )

    # 5. budget exhausted — the structural catch-all.
    if status == _EXHAUSTED_STATUS:
        return Trouble("budget_exhausted", "iteration budget exhausted")

    return NO_TROUBLE


__all__ = ["NO_TROUBLE", "Trouble", "TroubleKind", "detect_trouble"]

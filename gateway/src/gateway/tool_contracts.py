"""Deterministic per-tool contract checks — the ladder's "tools" rung.

The judged e2e scenarios answer *"will this model reach for the right
tool?"*. That question is model-dependent and worth measuring per model.
It is also the wrong instrument for *"does this tool work and return what
it claims?"* — that answer doesn't vary by model, so paying a live model
(and inheriting its flakiness) to find out is waste. Measured on the seed
set: 7 of 34 registered tools were ever exercised, and a judged scenario
per tool would mean a slow, expensive, non-deterministic suite.

So: this layer calls each tool directly, with no model and no tunnel, and
asserts two things.

* **Happy path** — valid arguments produce ``ToolResult`` with
  ``is_error=False``, plus whatever side effect the check declares.
* **Invalid arguments produce a structured error, not an exception.**
  This is the more valuable half. The agent loop feeds a tool's error
  string back to the model as a tool result so it can retry or give up; a
  tool that *raises* instead escapes that path and takes down the turn.
  No judged scenario reliably reveals it, because a model rarely sends
  malformed arguments on purpose.

Pure over an injected registry and context, so the runner is unit-testable
against a fake registry (one well-behaved tool, one raiser) without 34
live calls.

See `.kiro/specs/e2e-full-coverage/design.md` (D2).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .tools import Tool, ToolRegistry, ToolResult

# ctx -> args to call the tool with. A callable (not a dict) because
# valid arguments usually reference fixtures created for the check.
ArgsFactory = Callable[[Any], dict[str, Any]]
# (result, ctx) -> None; raises AssertionError when the side effect is missing.
SideEffectAssert = Callable[[ToolResult, Any], Awaitable[None] | None]


@dataclass(frozen=True)
class ContractCheck:
    """How to exercise one tool without a model."""

    tool: str
    valid_args: ArgsFactory
    side_effect: SideEffectAssert | None = None
    invalid_args: dict[str, Any] | None = field(default=None)
    """Arguments that must yield a structured error. ``None`` skips the
    invalid-args half (a tool taking no arguments has no way to be
    called wrongly)."""

    skip_reason: str = ""
    """Set to record a check that exists but can't run here (e.g. needs
    a live remote). Reported as skipped, never as passing."""

    known_broken: str = ""
    """A defect we've found, understood, and not yet fixed.

    The check still runs and its real outcome is reported; it just
    doesn't fail the suite. This exists so a coverage layer can be
    adopted without either hiding the bugs it finds or blocking CI on
    them — and so the entry is a standing reminder with a reason
    attached, rather than a commented-out check nobody revisits. If a
    known-broken check starts *passing*, that's reported too: the
    entry should then be removed."""


@dataclass(frozen=True)
class ContractResult:
    """Outcome for one tool."""

    tool: str
    passed: bool
    detail: str
    skipped: bool = False
    known_broken: str = ""

    @property
    def status(self) -> str:
        if self.skipped:
            return "skip"
        if self.known_broken and not self.passed:
            return "known-broken"
        if self.known_broken and self.passed:
            return "FIXED?"
        return "pass" if self.passed else "FAIL"

    @property
    def counts_as_failure(self) -> bool:
        """Whether the suite should go red for this."""
        return not self.passed and not self.skipped and not self.known_broken


@dataclass(frozen=True)
class ContractReport:
    """All checks, plus which registered tools had none."""

    results: list[ContractResult]
    unchecked: list[str]

    @property
    def failed(self) -> list[ContractResult]:
        return [r for r in self.results if r.counts_as_failure]

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed and not r.skipped)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def known_broken(self) -> list[ContractResult]:
        """Understood, unfixed defects — visible, not failing."""
        return [r for r in self.results if r.known_broken and not r.passed]

    @property
    def unexpectedly_fixed(self) -> list[ContractResult]:
        """Marked known-broken but passing now; drop the marker."""
        return [r for r in self.results if r.known_broken and r.passed]

    def render(self) -> str:
        lines = [
            f"Tool contracts: {self.passed_count} passed, "
            f"{len(self.failed)} failed, {len(self.known_broken)} known-broken, "
            f"{self.skipped_count} skipped, "
            f"{len(self.unchecked)} registered tools with no check",
        ]
        for r in sorted(self.results, key=lambda r: r.tool):
            lines.append(f"  - {r.tool}: {r.status}  ({r.detail})")
        if self.unchecked:
            lines.append("  no check: " + ", ".join(sorted(self.unchecked)))
        return "\n".join(lines)


async def run_contract_check(check: ContractCheck, tool: Tool, ctx: Any) -> ContractResult:
    """Exercise one tool. Never raises — a raising tool is the finding."""
    if check.skip_reason:
        return ContractResult(check.tool, True, check.skip_reason, skipped=True)
    outcome = await _exercise(check, tool, ctx)
    if check.known_broken:
        return ContractResult(
            outcome.tool,
            outcome.passed,
            f"{outcome.detail} [known: {check.known_broken}]",
            known_broken=check.known_broken,
        )
    return outcome


async def _exercise(check: ContractCheck, tool: Tool, ctx: Any) -> ContractResult:

    # Happy path.
    try:
        args = check.valid_args(ctx)
    except Exception as exc:
        return ContractResult(check.tool, False, f"fixture setup raised: {exc!r}")
    try:
        result = await tool.callable(args, ctx)
    except Exception as exc:
        return ContractResult(check.tool, False, f"raised on valid args: {exc!r}")
    if not isinstance(result, ToolResult):
        return ContractResult(
            check.tool, False, f"returned {type(result).__name__}, not ToolResult"
        )
    if result.is_error:
        return ContractResult(check.tool, False, f"errored on valid args: {result.payload[:200]}")

    if check.side_effect is not None:
        try:
            outcome = check.side_effect(result, ctx)
            if outcome is not None:
                await outcome
        except AssertionError as exc:
            return ContractResult(check.tool, False, f"side effect missing: {exc}")
        except Exception as exc:
            return ContractResult(check.tool, False, f"side-effect check raised: {exc!r}")

    # Invalid arguments must come back as a structured error. A raise here
    # would escape the agent loop's error handling and kill the turn.
    if check.invalid_args is not None:
        try:
            bad = await tool.callable(dict(check.invalid_args), ctx)
        except Exception as exc:
            return ContractResult(
                check.tool,
                False,
                f"raised on invalid args instead of returning an error: {exc!r}",
            )
        if not isinstance(bad, ToolResult):
            return ContractResult(
                check.tool, False, f"invalid args returned {type(bad).__name__}, not ToolResult"
            )
        if not bad.is_error:
            return ContractResult(
                check.tool, False, "invalid args reported success instead of an error"
            )

    return ContractResult(check.tool, True, "ok + structured error on bad args")


async def run_contract_checks(
    registry: ToolRegistry,
    ctx: Any,
    checks: list[ContractCheck],
    *,
    exempt: dict[str, str] | None = None,
) -> ContractReport:
    """Run every check whose tool is registered, and report the rest.

    A check for an unregistered tool is skipped with a reason rather than
    failed: retrieval tools only exist when retrieval is configured, and
    a switched-off feature is a deployment fact, not a defect (the same
    distinction the judged layer makes with ``requires_tools``)."""
    exempt = exempt or {}
    results: list[ContractResult] = []
    checked: set[str] = set()

    for check in checks:
        checked.add(check.tool)
        if not registry.has(check.tool):
            results.append(
                ContractResult(
                    check.tool,
                    True,
                    "not registered on this deployment — feature off, not broken",
                    skipped=True,
                )
            )
            continue
        results.append(await run_contract_check(check, registry.lookup(check.tool), ctx))

    unchecked = [
        name for name in registry.list_names() if name not in checked and name not in exempt
    ]
    return ContractReport(results=results, unchecked=unchecked)

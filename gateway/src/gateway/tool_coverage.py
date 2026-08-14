"""Which tools have *any* check, and which kind — the coverage view.

Answers one question a person kept having to answer by hand: "we said 7
of 34 tools were exercised; what is it now?" Counting by eye is how a
newly registered tool stays invisible for weeks, so this derives the
answer from the registry rather than from a number in a document.

**Scope, stated up front because the number is easy to over-read.** The
denominator is the tool registry. FITT's cross-cutting subsystems — auth,
cost accounting, fallback routing, approval-policy resolution, the
HMAC audit chain, rate limiting, boot warnings, the startup hooks, the
CLI, Open WebUI — are not tools and therefore cannot appear here at all.
"0 uncovered" means "every registered tool has a check", not "FITT is
covered". An audit on 2026-08-13 found the infrastructure spine largely
unmeasured while this reported 0 uncovered, so the render says so out
loud rather than leaving the reader to infer it.

Two axes, deliberately not summed into one percentage:

* **contract** — a deterministic offline check calls the tool directly
  (:mod:`gateway.tool_contracts`). Cheap, runs in CI, says the tool
  *works*.
* **judged** — a scenario declares it means to drive this tool
  (``TaskScenario.exercises_tools``). Says a model *chose* it.

They measure different things and neither substitutes for the other: a
contract check can't tell you the model will ever reach for the tool, and
a judged scenario can't tell you the tool returns a structured error for
bad arguments. A tool with both is well covered; with neither, untested.

**Intent is not evidence.** The judged axis counts what a scenario *aims*
at. If the model ignores the tool the scenario may still pass by another
route, and this report will not notice — the per-scenario matrix
(``fitt eval matrix``) is where what actually happened lives. Both
numbers are only honest together, which is why the render says so.

See `.kiro/specs/e2e-full-coverage/design.md` (D1) — Property 1 is that
registering a tool makes it appear here with no other edit.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CoverageEntry:
    """How one registered tool is covered."""

    tool: str
    contract: bool
    judged: bool
    exempt: str = ""
    """Non-empty when we deliberately chose not to check it here, with the
    reason. Keeps "decided against" distinguishable from "forgot" — the
    whole point of tracking coverage at all."""

    @property
    def covered(self) -> bool:
        return self.contract or self.judged or bool(self.exempt)

    @property
    def status(self) -> str:
        if self.contract and self.judged:
            return "contract+judged"
        if self.contract:
            return "contract"
        if self.judged:
            return "judged"
        if self.exempt:
            return "exempt"
        return "UNCOVERED"


@dataclass(frozen=True)
class CoverageReport:
    entries: list[CoverageEntry]
    orphan_checks: list[str]
    """Checks or scenario intents naming a tool that isn't registered.

    Usually a rename: the check quietly stops running and nothing goes
    red, because a check for a missing tool is skipped on purpose (a
    switched-off feature is a deployment fact, not a defect). Surfaced
    here so the two cases don't look alike."""

    off_deployment: list[str] = field(default_factory=list)
    """Named by a check or scenario, absent *by configuration*.

    ``memory_search`` only registers when ``memory.embedding_alias`` is
    bound, so on a retrieval-off deployment it is missing for a reason
    rather than by mistake. Kept apart from the orphans because that
    distinction is the same one the judged layer draws with
    ``requires_tools`` — and getting it wrong is what once graded three
    models down for a switched-off feature."""

    @property
    def uncovered(self) -> list[CoverageEntry]:
        return [e for e in self.entries if not e.covered]

    @property
    def contract_count(self) -> int:
        return sum(1 for e in self.entries if e.contract)

    @property
    def judged_count(self) -> int:
        return sum(1 for e in self.entries if e.judged)

    @property
    def exempt_entries(self) -> list[CoverageEntry]:
        return [e for e in self.entries if e.exempt and not e.contract and not e.judged]

    def render(self) -> str:
        total = len(self.entries)
        lines = [
            f"Tool coverage: {total} registered tools — "
            f"{self.contract_count} contract-checked, "
            f"{self.judged_count} named by a judged scenario, "
            f"{len(self.exempt_entries)} exempt, "
            f"{len(self.uncovered)} uncovered",
            "",
            "SCOPE — this counts TOOLS ONLY. The denominator is the tool "
            "registry, so '0 uncovered' says nothing about FITT's "
            "cross-cutting subsystems: auth, cost accounting, fallback "
            "routing, approval-policy resolution, the audit chain, rate "
            "limiting, boot warnings, startup hooks, the CLI, Open WebUI. "
            "None of those is a tool, so none can appear here — by "
            "construction, not by oversight.",
            "",
            "The judged column is INTENT, not evidence: it means a scenario "
            "aims to drive the tool, not that a model did. See "
            "`fitt eval matrix` for what actually happened.",
            "",
        ]
        for e in sorted(self.entries, key=lambda e: (e.covered, e.tool)):
            suffix = f"  ({e.exempt})" if e.exempt else ""
            lines.append(f"  - {e.tool}: {e.status}{suffix}")
        if self.off_deployment:
            lines += [
                "",
                "Not registered on this deployment (feature off, checked "
                "elsewhere): " + ", ".join(sorted(self.off_deployment)),
            ]
        if self.orphan_checks:
            lines += [
                "",
                "Checks/intents naming an unregistered tool (a rename would "
                "look exactly like this): " + ", ".join(sorted(self.orphan_checks)),
            ]
        return "\n".join(lines)


def build_coverage(
    registered: Collection[str],
    *,
    contract_checked: Collection[str],
    judged_intent: Collection[str],
    exempt: Mapping[str, str] | None = None,
    conditional: Collection[str] = (),
) -> CoverageReport:
    """Fold the registry against what checks and scenarios claim.

    Driven off ``registered`` so a new tool shows up as UNCOVERED with no
    edit here (Property 1). Everything is a plain collection of names, so
    this is pure and testable without building a registry.

    ``conditional`` names tools that exist only on some deployments; when
    absent they're reported as off-deployment rather than as orphans.
    Callers derive it rather than hand-maintain it: a scenario declaring
    ``requires_tools`` is already stating "this may not be here"."""
    exempt = exempt or {}
    checked = set(contract_checked)
    intent = set(judged_intent)
    names = set(registered)
    may_be_absent = set(conditional)

    entries = [
        CoverageEntry(
            tool=name,
            contract=name in checked,
            judged=name in intent,
            exempt=exempt.get(name, ""),
        )
        for name in sorted(names)
    ]
    missing = (checked | intent) - names
    return CoverageReport(
        entries=entries,
        orphan_checks=sorted(missing - may_be_absent),
        off_deployment=sorted(missing & may_be_absent),
    )

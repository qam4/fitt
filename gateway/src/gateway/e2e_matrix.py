"""The tracked feature/model standing — which models can drive what.

`fitt eval e2e` measures one model against the seed scenarios and writes
a JSON sidecar next to its markdown report. This module folds the latest
sidecar per DUT into a grid: scenarios down the side, models across the
top. That's the artifact worth keeping — a chat-window table gets
re-derived and drifts; this regenerates from stored runs.

Two distinctions the grid is careful about, both learned from getting
them wrong:

* **Not measured is not a failure.** A model that never ran a scenario
  (added after its last run) shows ``-``, never ``FAIL``. Otherwise
  adding a scenario silently downgrades every model that predates it.
* **Not scored is not a failure either.** ``unsupported`` (the
  deployment lacks a required tool) and ``inconclusive`` (the run didn't
  exercise what it tests) are their own cells. Collapsing them into
  ``FAIL`` is exactly how a switched-off feature and a leaked lesson got
  read as model defects.

Pure except for :func:`load_sidecars`, so the folding and rendering are
unit-testable against dicts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CELL_PASS = "pass"
CELL_FAIL = "FAIL"
CELL_UNSUPPORTED = "n/a"
CELL_INCONCLUSIVE = "?"
CELL_MISSING = "-"

_STATUS_CELLS = {
    "unsupported": CELL_UNSUPPORTED,
    "inconclusive": CELL_INCONCLUSIVE,
}


@dataclass(frozen=True)
class Standing:
    """One folded view: scenarios x DUTs, plus per-DUT provenance."""

    scenarios: list[str]
    duts: list[str]
    cells: dict[tuple[str, str], str]  # (scenario, dut) -> cell
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)  # dut -> sidecar

    def cell(self, scenario: str, dut: str) -> str:
        return self.cells.get((scenario, dut), CELL_MISSING)

    def stale_duts(self) -> list[str]:
        """DUTs missing at least one scenario the others have measured —
        i.e. last run predates a scenario-set change, so their column
        can't be compared like-for-like."""
        return [
            d for d in self.duts if any(self.cell(s, d) == CELL_MISSING for s in self.scenarios)
        ]


def load_sidecars(eval_dir: Path) -> list[dict[str, Any]]:
    """Read every e2e sidecar in ``eval_dir``, newest last.

    Unreadable or foreign JSON is skipped rather than fatal: the eval
    directory also holds alias-eval and profile sidecars."""
    out: list[dict[str, Any]] = []
    if not eval_dir.exists():
        return out
    for path in sorted(eval_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "scenarios" not in data or "dut" not in data:
            continue
        out.append(data)
    return sorted(out, key=lambda d: str(d.get("ts", "")))


def latest_per_dut(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Keep only each DUT's most recent run (input sorted oldest first)."""
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        latest[str(run["dut"])] = run
    return latest


def build_standing(runs: list[dict[str, Any]]) -> Standing:
    """Fold sidecars into the grid."""
    latest = latest_per_dut(runs)
    cells: dict[tuple[str, str], str] = {}
    scenario_order: list[str] = []

    for dut, run in latest.items():
        for entry in run.get("scenarios", []):
            name = str(entry.get("scenario", ""))
            if not name:
                continue
            if name not in scenario_order:
                scenario_order.append(name)
            status = str(entry.get("status", "scored"))
            if status in _STATUS_CELLS:
                cells[(name, dut)] = _STATUS_CELLS[status]
            else:
                passed = str(entry.get("objective", "fail")) == "pass"
                cells[(name, dut)] = CELL_PASS if passed else CELL_FAIL

    return Standing(
        scenarios=scenario_order,
        duts=sorted(latest),
        cells=cells,
        runs=latest,
    )


def render_standing(standing: Standing) -> str:
    """Markdown table + a legend + per-DUT provenance."""
    if not standing.duts:
        return (
            "No e2e runs found. Run `fitt eval e2e --dut <alias> --out "
            "<path>` first; each run writes the sidecar this reads.\n"
        )

    header = "| Feature (scenario) | " + " | ".join(standing.duts) + " |"
    sep = "|---" * (len(standing.duts) + 1) + "|"
    lines = [header, sep]
    for scenario in standing.scenarios:
        row = [standing.cell(scenario, d) for d in standing.duts]
        lines.append(f"| {scenario} | " + " | ".join(row) + " |")

    lines += [
        "",
        f"Legend: `{CELL_PASS}` objective check passed · `{CELL_FAIL}` failed · "
        f"`{CELL_UNSUPPORTED}` feature not available on this deployment · "
        f"`{CELL_INCONCLUSIVE}` ran but didn't exercise what it tests · "
        f"`{CELL_MISSING}` not measured for this model.",
        "",
        "Runs folded in (latest per model):",
        "",
    ]
    judges: set[str] = set()
    for dut in standing.duts:
        run = standing.runs[dut]
        judge = run.get("judge_model") or ("unknown" if run.get("judge_command") else "no judge")
        judges.add(str(judge))
        lines.append(
            f"- `{dut}`"
            + (f" ({run['model']})" if run.get("model") else "")
            + f" — {run.get('ts', 'unknown time')}, samples={run.get('samples', 1)}, "
            + f"objective {run.get('objective_passed', '?')}/{run.get('total', '?')}, "
            + f"judge: {judge}"
        )

    graded = judges - {"no judge"}
    if len(graded) > 1:
        lines += [
            "",
            "Judge verdicts in this table came from more than one model "
            f"({', '.join(sorted(graded))}), so the *judge* columns aren't "
            "comparable across models. Objective results are unaffected — "
            "they never involve a judge.",
        ]
    if "unpinned" in graded:
        lines += [
            "",
            "At least one run used an unpinned judge (`--model auto`), whose "
            "default moves between runs. Re-run it with an explicit model "
            "before citing its judge score.",
        ]

    stale = standing.stale_duts()
    if stale:
        lines += [
            "",
            "Not comparable like-for-like — these models are missing at least "
            "one scenario the others have measured, so re-run them before "
            f"reading the columns against each other: {', '.join(stale)}.",
        ]

    return "\n".join(lines) + "\n"

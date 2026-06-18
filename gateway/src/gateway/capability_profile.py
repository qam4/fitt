"""Phase 12 task 24 — per-alias capability profile (the thin profiler).

What this is for
----------------

The profiler's real job is **regression-catching**, not discovery. When
you swap or update the model behind an alias, the profile answers "did
anything degrade vs the last known-good?" — the granite case made
concrete (declared "supports tools: yes", wrong in practice under FITT's
real prompt). The grander "discover what every model can do" framing is
where profilers rot: built once, run once, never looked at again. So the
design centres on a stored baseline and a cheap re-run that **diffs**
against it.

Design commitments (from the Phase 12 design discussion, 2026-06-16)
--------------------------------------------------------------------

* **Declared vs measured are structurally separate.** Declared facts
  (context window, thinking, vision) are free and static (Ollama
  ``/api/tags``, models.dev) — trust them at catalog level, no more.
  Measured grades cost tokens, drift, and depend on prompt size — they
  are the whole point (the catalog said "tools: yes"; measurement found
  the truth). Mixing them hides which is which.

* **Capability and cost are separate fields, never a blended score.**
  Each :class:`MeasuredGrade` carries ``pass_rate`` AND latency/tokens
  side by side. "92% @ 4s" vs "96% @ 40s" is a real operator decision; a
  blended 0.85 hides the tradeoff. For a *local* model latency IS the
  cost (no per-token bill — the price is the user waiting and the GPU
  busy); for a paid model tokens are the cost (and a pressure toward good
  context summarization / compaction). We surface both.

* **Latency is measured warm; cold-load is recorded separately.** A
  thinking model that cold-loads ~9GB is slandered if the load time
  pollutes its per-call latency. Warm it, then measure; note cold-load on
  its own.

* **Facts, not verdicts.** The profile reports raw numbers (``p50 = 40s``)
  and lets the operator judge them against what the alias is *for* —
  40s/turn is terrible for an interactive Telegram reply, fine for a 6am
  cron digest. We don't grade "slow = bad".

* **Operator-in-the-loop.** The profile *informs/recommends* config; it
  never auto-drives runtime behaviour (design.md "two clocks"). A noisy,
  sample-limited measurement steering live behaviour is the silent-
  degradation failure we avoid.

* **Model-agnostic.** Grades key off measured pass-rates surfaced per
  alias; nothing branches on a model name.

Extensibility is the point
--------------------------

The named dimensions (tool-calling, coding, thinking, context window,
latency, tokens, VRAM, ...) are a *menu*, not a schema. A profile is a
**list** of declared facts + a **list** of measured grades, so adding a
dimension is appending an entry, never a record migration. The renderer
and :meth:`CapabilityProfile.diff` iterate the lists, so they handle any
dimension set unchanged. Fields the model anticipates but doesn't yet
populate (VRAM, per-grade tokens) sit ``None`` until their probe is wired
— adding them later is wiring, not redesign.

This module is the **pure layer**: data model + grading + diff + render,
fully unit-testable with no live model. The live producer (run the evals,
read Ollama metadata, build a profile) is a thin step on top, wired
separately.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

DimensionKind = Literal["declared", "measured", "resource"]


# --------------------------------------------------------------- facts


@dataclass(frozen=True, slots=True)
class DeclaredFact:
    """A free, static fact from the model catalog.

    ``value`` is stringified so the renderer and diff treat every fact
    uniformly. ``source`` records provenance (``ollama`` / ``models.dev``)
    so the operator knows how much to trust it — declared facts inform but
    do not certify (the granite case)."""

    name: str
    value: str
    source: str = "ollama"


@dataclass(frozen=True, slots=True)
class MeasuredGrade:
    """A measured behavioural dimension for one alias.

    Capability (``pass_rate`` over *valid* samples — transient infra
    failures excluded, mirroring the multi-sample contract) and cost
    (latency, tokens) are SEPARATE fields. ``pass_rate`` is ``None`` when
    every sample was transient (no signal). Latency is warm p50/p95 in
    seconds; token averages are per-valid-sample. Any cost field may be
    ``None`` when the source eval doesn't capture it yet (e.g. the
    alias-eval suites record latency but not tokens) — the data model is
    ready; populating it is wiring."""

    name: str
    pass_rate: float | None
    passes: int
    valid: int
    samples: int
    p50_latency_s: float | None = None
    p95_latency_s: float | None = None
    avg_in_tokens: float | None = None
    avg_out_tokens: float | None = None
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """Resource facts. Hybrid: ``declared_size_bytes`` is free from the
    catalog; ``resident_vram_mb`` and ``cold_load_s`` are measured at load
    (``ollama ps`` / nvidia-smi). All ``None`` in v1 until the probe is
    wired — the fields exist so adding the probe is wiring, not a model
    change. On a small-VRAM host this is the gate: does the model fit with
    headroom or crowd out everything else."""

    resident_vram_mb: int | None = None
    cold_load_s: float | None = None
    declared_size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class CapabilityProfile:
    """A per-alias profile: declared facts + measured grades + resources.

    Declared and measured are separate *lists* (extensibility: append, no
    migration). ``captured_at`` stamps the run so a stored baseline can be
    diffed against a fresh one."""

    alias: str
    model_id: str | None
    captured_at: datetime
    declared: list[DeclaredFact] = field(default_factory=list)
    measured: list[MeasuredGrade] = field(default_factory=list)
    resource: ResourceUsage | None = None

    def measured_by_name(self) -> dict[str, MeasuredGrade]:
        return {g.name: g for g in self.measured}

    def declared_by_name(self) -> dict[str, str]:
        return {f.name: f.value for f in self.declared}

    def diff(self, baseline: CapabilityProfile) -> ProfileDiff:
        """Diff this (new) profile against a ``baseline`` (last known-good).

        The regression-catcher: per measured dimension, the pass-rate and
        latency deltas; dimensions added/removed between runs; and changed
        declared facts (e.g. a model swap changed the context window).
        ``self`` is "after", ``baseline`` is "before"."""
        new_m = self.measured_by_name()
        old_m = baseline.measured_by_name()

        pass_rate_deltas: dict[str, float] = {}
        latency_deltas: dict[str, float] = {}
        for name in new_m.keys() & old_m.keys():
            new_g, old_g = new_m[name], old_m[name]
            if new_g.pass_rate is not None and old_g.pass_rate is not None:
                delta = round(new_g.pass_rate - old_g.pass_rate, 4)
                if delta != 0:
                    pass_rate_deltas[name] = delta
            if new_g.p50_latency_s is not None and old_g.p50_latency_s is not None:
                ldelta = round(new_g.p50_latency_s - old_g.p50_latency_s, 4)
                if ldelta != 0:
                    latency_deltas[name] = ldelta

        added = sorted(new_m.keys() - old_m.keys())
        removed = sorted(old_m.keys() - new_m.keys())

        old_decl = baseline.declared_by_name()
        new_decl = self.declared_by_name()
        declared_changes: dict[str, tuple[str | None, str | None]] = {}
        for name in old_decl.keys() | new_decl.keys():
            before, after = old_decl.get(name), new_decl.get(name)
            if before != after:
                declared_changes[name] = (before, after)

        return ProfileDiff(
            alias=self.alias,
            pass_rate_deltas=pass_rate_deltas,
            latency_deltas=latency_deltas,
            added_dimensions=added,
            removed_dimensions=removed,
            declared_changes=declared_changes,
        )


@dataclass(frozen=True, slots=True)
class ProfileDiff:
    """Result of diffing a fresh profile against a baseline.

    ``pass_rate_deltas`` / ``latency_deltas`` are ``new - old`` per
    dimension present in both. A negative pass-rate delta is a capability
    regression; a positive latency delta is a speed regression."""

    alias: str
    pass_rate_deltas: dict[str, float]
    latency_deltas: dict[str, float]
    added_dimensions: list[str]
    removed_dimensions: list[str]
    declared_changes: dict[str, tuple[str | None, str | None]]

    def regressions(self, *, pass_rate_drop: float = 0.1) -> list[str]:
        """Dimensions whose pass-rate dropped by more than ``pass_rate_drop``
        (default 10 points). The operator-facing "something got worse"
        signal — the reason to run the profiler on a model swap."""
        return sorted(
            name for name, delta in self.pass_rate_deltas.items() if delta <= -pass_rate_drop
        )

    @property
    def has_changes(self) -> bool:
        return bool(
            self.pass_rate_deltas
            or self.latency_deltas
            or self.added_dimensions
            or self.removed_dimensions
            or self.declared_changes
        )


# --------------------------------------------------------------- grading


def percentile(values: Sequence[float], p: float) -> float | None:
    """Nearest-rank percentile (``p`` in [0, 100]). ``None`` for empty.

    Nearest-rank (not interpolated) because sample counts are tiny (k=5);
    interpolation would imply a precision we don't have."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(1, min(len(ordered), math.ceil(p / 100.0 * len(ordered))))
    return ordered[rank - 1]


def grade_from_samples(
    name: str,
    *,
    passes: int,
    valid: int,
    samples: int,
    latencies_ms: Sequence[float] = (),
    in_tokens: Sequence[float] = (),
    out_tokens: Sequence[float] = (),
    notes: str = "",
) -> MeasuredGrade:
    """Build a :class:`MeasuredGrade` from raw per-sample numbers.

    Eval-source-agnostic on purpose: the alias-eval and scenario-eval
    runners produce different result shapes, but both can hand this their
    primitives. ``pass_rate`` is ``passes / valid`` (``None`` when no valid
    samples). Latency p50/p95 are seconds (input ms / 1000). Token averages
    are over whatever lists are supplied (empty -> ``None``)."""
    pass_rate = passes / valid if valid else None
    p50_ms = percentile(latencies_ms, 50)
    p95_ms = percentile(latencies_ms, 95)
    return MeasuredGrade(
        name=name,
        pass_rate=pass_rate,
        passes=passes,
        valid=valid,
        samples=samples,
        p50_latency_s=round(p50_ms / 1000.0, 3) if p50_ms is not None else None,
        p95_latency_s=round(p95_ms / 1000.0, 3) if p95_ms is not None else None,
        avg_in_tokens=round(sum(in_tokens) / len(in_tokens), 1) if in_tokens else None,
        avg_out_tokens=round(sum(out_tokens) / len(out_tokens), 1) if out_tokens else None,
        notes=notes,
    )


# --------------------------------------------------------------- render


def _fmt_rate(rate: float | None) -> str:
    return f"{rate * 100:.0f}%" if rate is not None else "n/a"


def _fmt_latency(s: float | None) -> str:
    return f"{s:.1f}s" if s is not None else "-"


def render_profile_markdown(profile: CapabilityProfile) -> str:
    """Human-first profile report. Declared and measured kept in separate
    sections (different trust levels); capability and cost as separate
    columns (the operator reads the tradeoff). No blended score, no
    'slow = bad' verdict — raw numbers the operator judges against the
    alias's purpose."""
    lines: list[str] = []
    lines.append(f"# Capability profile — {profile.alias}")
    lines.append("")
    lines.append(f"- model: `{profile.model_id or 'unknown'}`")
    lines.append(f"- captured: {profile.captured_at.isoformat()}")
    lines.append("")

    lines.append("## Declared (catalog — informs, does not certify)")
    lines.append("")
    if profile.declared:
        for f in profile.declared:
            lines.append(f"- **{f.name}**: {f.value}  _(source: {f.source})_")
    else:
        lines.append("_(none)_")
    lines.append("")

    lines.append("## Measured (capability + cost, separate)")
    lines.append("")
    if profile.measured:
        lines.append("| dimension | pass | samples | p50 | p95 | in tok | out tok |")
        lines.append("|-----------|------|---------|-----|-----|--------|---------|")
        for g in profile.measured:
            in_tok = f"{g.avg_in_tokens:.0f}" if g.avg_in_tokens is not None else "-"
            out_tok = f"{g.avg_out_tokens:.0f}" if g.avg_out_tokens is not None else "-"
            samp = f"{g.passes}/{g.valid}" + (
                f" (+{g.samples - g.valid} transient)" if g.samples != g.valid else ""
            )
            lines.append(
                f"| {g.name} | {_fmt_rate(g.pass_rate)} | {samp} | "
                f"{_fmt_latency(g.p50_latency_s)} | {_fmt_latency(g.p95_latency_s)} | "
                f"{in_tok} | {out_tok} |"
            )
        # Surface any per-dimension notes below the table.
        for g in profile.measured:
            if g.notes:
                lines.append(f"- _{g.name}_: {g.notes}")
    else:
        lines.append("_(none)_")
    lines.append("")

    if profile.resource is not None:
        r = profile.resource
        lines.append("## Resources")
        lines.append("")
        size_mb = (
            f"{r.declared_size_bytes / 1_048_576:.0f} MB"
            if r.declared_size_bytes is not None
            else "-"
        )
        lines.append(f"- declared size: {size_mb}")
        lines.append(
            f"- resident VRAM: {r.resident_vram_mb} MB"
            if r.resident_vram_mb is not None
            else "- resident VRAM: _(not measured)_"
        )
        lines.append(
            f"- cold-load: {r.cold_load_s:.1f}s"
            if r.cold_load_s is not None
            else "- cold-load: _(not measured)_"
        )
        lines.append("")

    return "\n".join(lines)


def render_diff_markdown(diff: ProfileDiff) -> str:
    """Render a baseline diff — the regression-catcher's output."""
    lines: list[str] = []
    lines.append(f"# Profile diff — {diff.alias}")
    lines.append("")
    if not diff.has_changes:
        lines.append("No changes vs baseline.")
        return "\n".join(lines)

    regressions = diff.regressions()
    if regressions:
        lines.append("## ⚠️ Regressions (pass-rate dropped >10 points)")
        lines.append("")
        for name in regressions:
            lines.append(f"- **{name}**: {diff.pass_rate_deltas[name] * 100:+.0f} points")
        lines.append("")

    if diff.pass_rate_deltas:
        lines.append("## Pass-rate deltas")
        lines.append("")
        for name, delta in sorted(diff.pass_rate_deltas.items()):
            lines.append(f"- {name}: {delta * 100:+.0f} points")
        lines.append("")

    if diff.latency_deltas:
        lines.append("## Latency deltas (p50)")
        lines.append("")
        for name, delta in sorted(diff.latency_deltas.items()):
            lines.append(f"- {name}: {delta:+.1f}s")
        lines.append("")

    if diff.added_dimensions or diff.removed_dimensions:
        lines.append("## Dimensions")
        lines.append("")
        for name in diff.added_dimensions:
            lines.append(f"- added: {name}")
        for name in diff.removed_dimensions:
            lines.append(f"- removed: {name}")
        lines.append("")

    if diff.declared_changes:
        lines.append("## Declared changes")
        lines.append("")
        for name, (before, after) in sorted(diff.declared_changes.items()):
            lines.append(f"- {name}: `{before}` -> `{after}`")
        lines.append("")

    return "\n".join(lines)

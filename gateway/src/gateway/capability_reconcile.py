"""Phase 12.5c — feature <-> capability reconciler (the recommendation).

The capability profile (Phase 12 task 24) measures *what a model can do*
per dimension. This module is the layer on top that answers *what should
I turn on* (roadmap Principle 12): given an alias's enabled features and
its measured profile, report each feature as ``satisfied`` /
``unsatisfied`` / ``unknown``.

Two design commitments, both from Principle 12 and the task-24
operator-in-the-loop stance:

* **Surfaces, never drives.** :func:`reconcile` reads config + profile and
  returns a verdict list. It mutates nothing, toggles no feature flag, and
  changes no runtime behaviour. Its only downstream effects are a rendered
  view (the Capability surface) and a boot ERROR log — never an
  auto-enable/disable. Measurements are multi-sample-noisy; steering live
  behaviour off a noisy read is the silent-degradation failure we avoid.

* **Model-agnostic, keyed on dimensions.** :func:`feature_requirements`
  maps a feature to the profile *dimension* it needs (planning ->
  ``plan-election``), never to a model name. Behaviour keys off measured
  capability, not "is this hermes3".

The **three-state** verdict is the point. ``unknown`` (the required
dimension wasn't measured for this alias) is first-class and distinct from
``unsatisfied`` (measured, and below the bar). ``unknown`` points the
operator at "Measure capability"; ``unsatisfied`` is the loud "you enabled
something this model can't drive". v1 measures one dimension
(``plan-election``, Stage 1), so any feature whose dimension isn't yet
measured reports ``unknown`` until that dimension ships — which is exactly
how the deferred profile dimensions earn their demand-ordered priority.

Layering:

* The top of the module is the **pure** core (dataclasses,
  :func:`feature_requirements`, :func:`reconcile`) — no IO, no ``Config``,
  fully unit-testable with hand-built profiles.
* The bottom is thin **wiring** (:func:`enabled_features_for_alias`,
  :func:`readiness_for_alias`, :func:`check_unsatisfied_features`) that
  reads config + loads on-disk profiles and hands the primitives to the
  pure core. The boot check mirrors :func:`gateway.config.check_missing_api_keys`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .capability_profile import CapabilityProfile, load_baseline

if TYPE_CHECKING:
    from .config import Config

# --------------------------------------------------------------- pure core

ReadinessStatus = Literal["satisfied", "unsatisfied", "unknown"]


@dataclass(frozen=True, slots=True)
class FeatureRequirement:
    """What a FITT feature needs from the capability profile to run well.

    ``dimension`` is a profile :class:`~gateway.capability_profile.MeasuredGrade`
    name (never a model name — the model-agnostic guarantee).
    ``min_pass_rate`` is the bar that dimension's pass-rate must clear.
    ``allow_planner_alias`` marks features that a configured
    ``planner_alias`` can satisfy on the executor's behalf (planning:
    "elects to plan on the alias OR its planner_alias")."""

    feature: str
    dimension: str
    min_pass_rate: float
    allow_planner_alias: bool = False


@dataclass(frozen=True, slots=True)
class FeatureReadiness:
    """One feature's reconciled verdict for one alias.

    ``status`` is exactly one of satisfied / unsatisfied / unknown.
    ``detail`` is a short human reason (``"plan-election 100% >= 50%"`` /
    ``"plan-election not measured - run Measure capability"``)."""

    feature: str
    status: ReadinessStatus
    detail: str


# The planning bar: an alias that elects to plan on at least half of a
# multi-step turn can drive orchestration. Below that, orchestration runs
# plan-less (the task-23 hermes3 shape: 0% election -> executor runs flat),
# which is the "silently dead feature" we surface. Tunable as measurement
# sharpens; a single named constant keeps the threshold discoverable.
_PLANNING_MIN_ELECTION = 0.5


def feature_requirements() -> list[FeatureRequirement]:
    """The static, model-agnostic feature -> dimension map.

    v1 defines the one feature whose dimension is measured today
    (``planning`` -> ``plan-election``, Stage 1) plus a forward-looking
    entry (``web_search_answer`` -> ``synthesis``) whose dimension isn't
    measured yet — so a caller that enables it gets a first-class
    ``unknown`` rather than a silent gap, and the missing dimension earns
    its priority (the relay-vs-synthesize finding is the synthesis
    dimension's demand). Adding a feature is appending an entry — no
    reconcile change."""
    return [
        FeatureRequirement(
            feature="planning",
            dimension="plan-election",
            min_pass_rate=_PLANNING_MIN_ELECTION,
            allow_planner_alias=True,
        ),
        # Forward-looking: the synthesis dimension isn't measured in v1,
        # so this reports `unknown` until it ships (design Decision 5).
        FeatureRequirement(
            feature="web_search_answer",
            dimension="synthesis",
            min_pass_rate=0.5,
        ),
    ]


def _assess(
    req: FeatureRequirement,
    profile: CapabilityProfile | None,
    planner_profile: CapabilityProfile | None,
) -> FeatureReadiness:
    """Reconcile one requirement against the available profile(s).

    Planning's ``allow_planner_alias`` is an OR: the feature is satisfied
    if EITHER the alias itself OR its planner_alias clears the bar on the
    dimension. A dimension measured nowhere is ``unknown`` (never a
    false-negative ``unsatisfied`` — Req 5.6); a ``None`` pass-rate (every
    sample transient) is also ``unknown`` (no signal)."""
    sources: list[tuple[str, CapabilityProfile]] = []
    if profile is not None:
        sources.append(("alias", profile))
    if req.allow_planner_alias and planner_profile is not None:
        sources.append(("planner_alias", planner_profile))

    measured: list[tuple[str, float]] = []
    for who, prof in sources:
        grade = prof.measured_by_name().get(req.dimension)
        if grade is not None and grade.pass_rate is not None:
            measured.append((who, grade.pass_rate))

    thr_pct = req.min_pass_rate * 100
    if not measured:
        return FeatureReadiness(
            req.feature,
            "unknown",
            f"{req.dimension} not measured - run Measure capability",
        )

    best_who, best_rate = max(measured, key=lambda t: t[1])
    pct = best_rate * 100
    if best_rate >= req.min_pass_rate:
        via = "" if best_who == "alias" else f" (via {best_who})"
        return FeatureReadiness(
            req.feature, "satisfied", f"{req.dimension} {pct:.0f}% >= {thr_pct:.0f}%{via}"
        )
    return FeatureReadiness(
        req.feature, "unsatisfied", f"{req.dimension} {pct:.0f}% < {thr_pct:.0f}%"
    )


def reconcile(
    enabled_features: Mapping[str, bool],
    profile: CapabilityProfile | None,
    *,
    planner_profile: CapabilityProfile | None = None,
) -> list[FeatureReadiness]:
    """Reconcile an alias's enabled features against its measured profile.

    Returns exactly one :class:`FeatureReadiness` per *enabled* feature
    (disabled features are skipped — nothing to warn about). A feature
    with no defined requirement reports ``unknown`` (we can't judge what we
    don't model). Totality + three-state is Property 3.

    Reads only; mutates nothing (Property 4). ``planner_profile`` is the
    profile of the alias's configured ``planner_alias`` (or ``None``); it
    lets a planning-capable planner satisfy planning on an executor that
    doesn't elect."""
    reqs = {r.feature: r for r in feature_requirements()}
    out: list[FeatureReadiness] = []
    for feature, enabled in enabled_features.items():
        if not enabled:
            continue
        req = reqs.get(feature)
        if req is None:
            out.append(
                FeatureReadiness(
                    feature, "unknown", "no capability requirement defined for this feature"
                )
            )
            continue
        out.append(_assess(req, profile, planner_profile))
    return out


# --------------------------------------------------------------- wiring (IO)


def enabled_features_for_alias(config: Config, alias: str) -> dict[str, bool]:
    """Map an alias's config to the reconciler's enabled-feature dict.

    v1 exposes the one per-alias gateable feature: ``planning`` is enabled
    iff the alias is opted into orchestration. As more features grow
    per-alias gates, they're added here (the reconciler's pure core is
    already general)."""
    return {"planning": config.is_orchestrated(alias)}


def readiness_for_alias(config: Config, alias: str, fitt_home: Path) -> list[FeatureReadiness]:
    """Load the alias's profile (and its planner_alias's profile, if
    configured) and reconcile. The IO glue shared by the dashboard
    Capability surface and the boot check."""
    profile = load_baseline(alias, fitt_home)
    planner_profile: CapabilityProfile | None = None
    ocfg = config.orchestration.get(alias)
    if ocfg is not None and ocfg.planner_alias:
        planner_profile = load_baseline(ocfg.planner_alias, fitt_home)
    return reconcile(
        enabled_features_for_alias(config, alias),
        profile,
        planner_profile=planner_profile,
    )


def check_unsatisfied_features(config: Config, fitt_home: Path) -> list[str]:
    """Return human-readable warnings for enabled-but-``unsatisfied``
    features across all aliases. Mirrors
    :func:`gateway.config.check_missing_api_keys`: the caller logs each at
    ERROR so the misconfiguration is unmissable, but it never blocks boot
    (fail-loud, not fail-closed - Req 5.4).

    Only ``unsatisfied`` (measured, below the bar) warns.  ``unknown``
    (not measured) is silent here - it isn't a misconfiguration, just an
    un-run measurement, surfaced on the dashboard with a "Measure
    capability" nudge rather than shouted at boot."""
    warnings: list[str] = []
    for alias in config.alias_names():
        for readiness in readiness_for_alias(config, alias, fitt_home):
            if readiness.status != "unsatisfied":
                continue
            warnings.append(
                f"alias {alias!r} has feature {readiness.feature!r} enabled but the "
                f"bound model doesn't satisfy it: {readiness.detail}. The feature will "
                f"run degraded (e.g. orchestration on a model that rarely elects to "
                f"plan runs plan-less). Re-measure, pick a stronger model, or set a "
                f"planner_alias."
            )
    return warnings


__all__ = [
    "FeatureReadiness",
    "FeatureRequirement",
    "ReadinessStatus",
    "check_unsatisfied_features",
    "enabled_features_for_alias",
    "feature_requirements",
    "readiness_for_alias",
    "reconcile",
]

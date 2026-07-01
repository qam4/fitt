"""Phase 12.5c — feature <-> capability reconciler tests.

Covers the pure core (totality + three-state Property 3, unknown on a
missing dimension, planner_alias borrow, never-mutates Property 4, a
hypothesis totality property) and the IO wiring (readiness_for_alias,
check_unsatisfied_features)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from gateway.capability_profile import CapabilityProfile, MeasuredGrade, write_profile
from gateway.capability_reconcile import (
    FeatureReadiness,
    check_unsatisfied_features,
    enabled_features_for_alias,
    feature_requirements,
    readiness_for_alias,
    reconcile,
)
from gateway.config import AliasOrchestrationConfig, Config, ModelConfig

# --------------------------------------------------------------- helpers


def _profile(alias: str, grades: dict[str, float | None]) -> CapabilityProfile:
    """Build a profile whose measured dimensions carry the given
    pass-rates (``None`` = all-transient, no signal)."""
    measured = [
        MeasuredGrade(
            name=name,
            pass_rate=rate,
            passes=int((rate or 0.0) * 5),
            valid=5 if rate is not None else 0,
            samples=5,
        )
        for name, rate in grades.items()
    ]
    return CapabilityProfile(
        alias=alias, model_id="m", captured_at=datetime.now(UTC), measured=measured
    )


def _config(orchestration: dict[str, AliasOrchestrationConfig] | None = None) -> Config:
    return Config(
        aliases={"fitt-default": "m1", "fitt-smart": "m2"},
        models=[
            ModelConfig(
                id="m1",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="qwen3:8b",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
            ModelConfig(
                id="m2",
                backend="openrouter",
                model="anthropic/claude-sonnet-4.5",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
        ],
        orchestration=orchestration or {},
    )


# --------------------------------------------------------------- pure core


def test_reconcile_skips_disabled_and_covers_enabled() -> None:
    """Exactly one readiness per *enabled* feature; disabled features
    produce nothing (Property 3 totality half)."""
    prof = _profile("a", {"plan-election": 1.0})
    out = reconcile({"planning": True, "web_search_answer": False}, prof)
    assert [r.feature for r in out] == ["planning"]


def test_planning_satisfied_when_election_clears_bar() -> None:
    out = reconcile({"planning": True}, _profile("a", {"plan-election": 1.0}))
    assert out[0].status == "satisfied"
    assert "plan-election" in out[0].detail


def test_planning_unsatisfied_when_below_bar() -> None:
    """0% election (the task-23 hermes3 shape) is a loud unsatisfied."""
    out = reconcile({"planning": True}, _profile("a", {"plan-election": 0.0}))
    assert out[0].status == "unsatisfied"


def test_unknown_when_dimension_not_measured() -> None:
    """A required dimension absent from the profile is `unknown`, never a
    false-negative `unsatisfied` (Req 5.6)."""
    out = reconcile({"planning": True}, _profile("a", {"coding": 1.0}))
    assert out[0].status == "unknown"
    assert "Measure capability" in out[0].detail


def test_unknown_when_no_profile() -> None:
    out = reconcile({"planning": True}, None)
    assert out[0].status == "unknown"


def test_unknown_when_pass_rate_is_none() -> None:
    """All-transient samples (pass_rate None) carry no signal -> unknown,
    not a spurious unsatisfied."""
    out = reconcile({"planning": True}, _profile("a", {"plan-election": None}))
    assert out[0].status == "unknown"


def test_enabled_feature_without_requirement_is_unknown() -> None:
    out = reconcile({"totally-made-up-feature": True}, _profile("a", {}))
    assert out[0].status == "unknown"


def test_planner_alias_borrow_satisfies_planning() -> None:
    """An executor that never elects (0%) is satisfied for planning when
    its planner_alias elects reliably — the OR borrow."""
    executor = _profile("exec", {"plan-election": 0.0})
    planner = _profile("planner", {"plan-election": 1.0})
    out = reconcile({"planning": True}, executor, planner_profile=planner)
    assert out[0].status == "satisfied"
    assert "via planner_alias" in out[0].detail


def test_planner_alias_not_borrowed_when_feature_disallows() -> None:
    """The borrow only applies to features flagged allow_planner_alias;
    planning is, so confirm the flag is actually set in the map."""
    reqs = {r.feature: r for r in feature_requirements()}
    assert reqs["planning"].allow_planner_alias is True
    assert reqs["web_search_answer"].allow_planner_alias is False


def test_reconcile_never_mutates_inputs() -> None:
    """Property 4: reconcile reads only. The enabled-features mapping and
    the profile are unchanged, and the outputs are frozen."""
    enabled = {"planning": True}
    prof = _profile("a", {"plan-election": 0.3})
    before_measured = list(prof.measured)

    out = reconcile(dict(enabled), prof)

    assert enabled == {"planning": True}
    assert prof.measured == before_measured
    # Frozen dataclass — attribute assignment raises.
    r = out[0]
    assert isinstance(r, FeatureReadiness)
    try:
        r.status = "satisfied"  # type: ignore[misc]
    except AttributeError:
        pass
    else:  # pragma: no cover - frozen dataclass must reject
        raise AssertionError("FeatureReadiness should be immutable")


@given(
    enabled=st.dictionaries(
        st.sampled_from(["planning", "web_search_answer", "mystery"]),
        st.booleans(),
        max_size=3,
    ),
    rate=st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0)),
)
def test_reconcile_totality_property(enabled: dict[str, bool], rate: float | None) -> None:
    """Property 3: for any (enabled features, profile), reconcile returns
    exactly one three-state status per enabled feature — total and
    well-formed."""
    prof = _profile("a", {"plan-election": rate})
    out = reconcile(enabled, prof)
    enabled_names = {f for f, on in enabled.items() if on}
    assert {r.feature for r in out} == enabled_names
    assert len(out) == len(enabled_names)
    for r in out:
        assert r.status in ("satisfied", "unsatisfied", "unknown")


# --------------------------------------------------------------- wiring (IO)


def test_enabled_features_tracks_orchestration_gate() -> None:
    off = _config()
    on = _config({"fitt-default": AliasOrchestrationConfig(enabled=True)})
    assert enabled_features_for_alias(off, "fitt-default") == {"planning": False}
    assert enabled_features_for_alias(on, "fitt-default") == {"planning": True}


def test_readiness_for_alias_reads_profile(tmp_path: Path) -> None:
    cfg = _config({"fitt-default": AliasOrchestrationConfig(enabled=True)})
    write_profile(_profile("fitt-default", {"plan-election": 1.0}), tmp_path)
    out = readiness_for_alias(cfg, "fitt-default", tmp_path)
    assert [(r.feature, r.status) for r in out] == [("planning", "satisfied")]


def test_readiness_for_alias_borrows_planner_profile(tmp_path: Path) -> None:
    """planner_alias's on-disk profile satisfies planning for an executor
    that doesn't elect."""
    cfg = _config(
        {"fitt-default": AliasOrchestrationConfig(enabled=True, planner_alias="fitt-smart")}
    )
    write_profile(_profile("fitt-default", {"plan-election": 0.0}), tmp_path)
    write_profile(_profile("fitt-smart", {"plan-election": 1.0}), tmp_path)
    out = readiness_for_alias(cfg, "fitt-default", tmp_path)
    assert out[0].status == "satisfied"
    assert "via planner_alias" in out[0].detail


def test_check_unsatisfied_features_warns_only_unsatisfied(tmp_path: Path) -> None:
    # Unsatisfied: orchestration on, election below bar.
    bad = _config({"fitt-default": AliasOrchestrationConfig(enabled=True)})
    write_profile(_profile("fitt-default", {"plan-election": 0.0}), tmp_path)
    warnings = check_unsatisfied_features(bad, tmp_path)
    assert len(warnings) == 1
    assert "fitt-default" in warnings[0] and "planning" in warnings[0]


def test_check_unsatisfied_features_silent_on_satisfied(tmp_path: Path) -> None:
    good = _config({"fitt-default": AliasOrchestrationConfig(enabled=True)})
    write_profile(_profile("fitt-default", {"plan-election": 1.0}), tmp_path)
    assert check_unsatisfied_features(good, tmp_path) == []


def test_check_unsatisfied_features_silent_on_unknown(tmp_path: Path) -> None:
    """Enabled but never profiled -> unknown, not a boot warning (the
    dashboard nudges "Measure capability" instead)."""
    unmeasured = _config({"fitt-default": AliasOrchestrationConfig(enabled=True)})
    assert check_unsatisfied_features(unmeasured, tmp_path) == []


def test_check_unsatisfied_features_silent_when_disabled(tmp_path: Path) -> None:
    """A disabled feature never warns, even with a failing profile."""
    disabled = _config()
    write_profile(_profile("fitt-default", {"plan-election": 0.0}), tmp_path)
    assert check_unsatisfied_features(disabled, tmp_path) == []

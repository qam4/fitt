"""Tests for :mod:`gateway.capability_profile` — the pure profiler layer
(Phase 12 task 24).

Pins the design commitments:

* grading math: pass_rate over valid samples, warm-latency percentiles,
  token averages, and the None-when-no-signal / None-when-not-captured
  contracts;
* the baseline diff (the regression-catcher): pass-rate + latency deltas,
  added/removed dimensions, declared changes, and the >10-point regression
  flag;
* render: declared and measured kept in separate sections; capability and
  cost as separate columns (no blended score).
"""

from __future__ import annotations

from datetime import UTC, datetime

from gateway.capability_profile import (
    CapabilityProfile,
    DeclaredFact,
    MeasuredGrade,
    ResourceUsage,
    grade_from_samples,
    percentile,
    render_diff_markdown,
    render_profile_markdown,
)


def _ts() -> datetime:
    return datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------- percentile


def test_percentile_empty_is_none() -> None:
    assert percentile([], 50) is None


def test_percentile_single() -> None:
    assert percentile([4.0], 95) == 4.0


def test_percentile_nearest_rank() -> None:
    vals = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(vals, 50) == 30.0
    assert percentile(vals, 95) == 50.0


# --------------------------------------------------------------- grading


def test_grade_pass_rate_and_latency() -> None:
    g = grade_from_samples(
        "tool-calling",
        passes=9,
        valid=10,
        samples=10,
        latencies_ms=[3000, 3100, 3200, 4000, 3050, 3000, 3300, 3100, 3900, 3000],
    )
    assert g.pass_rate == 0.9
    assert g.p50_latency_s is not None
    assert g.p95_latency_s is not None
    # Latency is in seconds, p95 >= p50.
    assert g.p95_latency_s >= g.p50_latency_s
    # No token lists supplied -> token fields stay None (capability and
    # cost are independent; absence is honest, not zero).
    assert g.avg_in_tokens is None
    assert g.avg_out_tokens is None


def test_grade_no_valid_samples_pass_rate_none() -> None:
    g = grade_from_samples("x", passes=0, valid=0, samples=3)
    assert g.pass_rate is None  # all transient -> no signal, not 0%
    assert g.p50_latency_s is None


def test_grade_token_averages() -> None:
    g = grade_from_samples(
        "orchestration",
        passes=2,
        valid=3,
        samples=3,
        in_tokens=[8000, 9000, 10000],
        out_tokens=[400, 600, 500],
    )
    assert g.avg_in_tokens == 9000.0
    assert g.avg_out_tokens == 500.0


# --------------------------------------------------------------- diff


def _profile(
    measured: list[MeasuredGrade], declared: list[DeclaredFact] | None = None
) -> CapabilityProfile:
    return CapabilityProfile(
        alias="fitt-ec2-hermes",
        model_id="hermes3:8b",
        captured_at=_ts(),
        declared=declared or [],
        measured=measured,
    )


def test_diff_detects_pass_rate_regression() -> None:
    baseline = _profile([MeasuredGrade("tool-calling", 0.9, 9, 10, 10, p50_latency_s=3.0)])
    fresh = _profile([MeasuredGrade("tool-calling", 0.6, 6, 10, 10, p50_latency_s=3.0)])
    diff = fresh.diff(baseline)
    assert diff.pass_rate_deltas["tool-calling"] == -0.3
    assert "tool-calling" in diff.regressions()
    assert diff.has_changes


def test_diff_small_drop_not_flagged_regression() -> None:
    baseline = _profile([MeasuredGrade("tool-calling", 0.9, 9, 10, 10)])
    fresh = _profile([MeasuredGrade("tool-calling", 0.85, 17, 20, 20)])
    diff = fresh.diff(baseline)
    # 5-point drop is below the 10-point regression threshold.
    assert diff.regressions() == []
    assert diff.pass_rate_deltas["tool-calling"] == -0.05


def test_diff_latency_delta() -> None:
    baseline = _profile([MeasuredGrade("coding", 0.7, 7, 10, 10, p50_latency_s=4.0)])
    fresh = _profile([MeasuredGrade("coding", 0.7, 7, 10, 10, p50_latency_s=6.5)])
    diff = fresh.diff(baseline)
    assert diff.latency_deltas["coding"] == 2.5


def test_diff_added_and_removed_dimensions() -> None:
    baseline = _profile([MeasuredGrade("tool-calling", 0.9, 9, 10, 10)])
    fresh = _profile([MeasuredGrade("coding", 0.7, 7, 10, 10)])
    diff = fresh.diff(baseline)
    assert diff.added_dimensions == ["coding"]
    assert diff.removed_dimensions == ["tool-calling"]


def test_diff_declared_changes() -> None:
    baseline = _profile(
        [],
        declared=[DeclaredFact("context_window", "40960"), DeclaredFact("thinking", "true")],
    )
    fresh = _profile(
        [],
        declared=[DeclaredFact("context_window", "131072"), DeclaredFact("thinking", "true")],
    )
    diff = fresh.diff(baseline)
    # Only the changed fact shows; the unchanged one does not.
    assert diff.declared_changes == {"context_window": ("40960", "131072")}


def test_diff_no_changes() -> None:
    g = [MeasuredGrade("tool-calling", 0.9, 9, 10, 10, p50_latency_s=3.0)]
    diff = _profile(list(g)).diff(_profile(list(g)))
    assert not diff.has_changes
    assert "No changes" in render_diff_markdown(diff)


def test_diff_none_pass_rate_not_compared() -> None:
    # A transient (all-None) fresh grade must not crash the diff or
    # fabricate a delta against a real baseline.
    baseline = _profile([MeasuredGrade("tool-calling", 0.9, 9, 10, 10)])
    fresh = _profile([MeasuredGrade("tool-calling", None, 0, 0, 5)])
    diff = fresh.diff(baseline)
    assert "tool-calling" not in diff.pass_rate_deltas


# --------------------------------------------------------------- render


def test_render_profile_separates_declared_and_measured() -> None:
    profile = CapabilityProfile(
        alias="fitt-ec2-hermes",
        model_id="hermes3:8b",
        captured_at=_ts(),
        declared=[
            DeclaredFact("context_window", "131072"),
            DeclaredFact("thinking", "false"),
        ],
        measured=[
            MeasuredGrade("tool-calling", 0.9, 9, 10, 10, p50_latency_s=3.1, p95_latency_s=4.0),
            MeasuredGrade("coding", 0.7, 7, 10, 10, p50_latency_s=5.2, avg_in_tokens=8000.0),
        ],
        resource=ResourceUsage(declared_size_bytes=4_661_227_243),
    )
    md = render_profile_markdown(profile)
    assert "## Declared" in md
    assert "## Measured" in md
    assert "context_window" in md
    assert "131072" in md
    # Capability and cost both present as separate columns.
    assert "90%" in md
    assert "3.1s" in md
    # No blended single score line.
    assert "overall score" not in md.lower()
    # Resource section renders declared size, VRAM not-measured.
    assert "Resources" in md
    assert "not measured" in md


def test_render_diff_flags_regression() -> None:
    baseline = _profile([MeasuredGrade("tool-calling", 0.9, 9, 10, 10)])
    fresh = _profile([MeasuredGrade("tool-calling", 0.6, 6, 10, 10)])
    md = render_diff_markdown(fresh.diff(baseline))
    assert "Regression" in md
    assert "tool-calling" in md


# --------------------------------------------------------------- declared facts


def test_declared_from_ollama_tags() -> None:
    from gateway.capability_profile import declared_from_ollama_tags

    payload = {
        "models": [
            {
                "name": "hermes3:8b",
                "size": 4_661_227_243,
                "details": {
                    "parameter_size": "8.0B",
                    "quantization_level": "Q4_0",
                    "context_length": 131072,
                },
                "capabilities": ["completion", "tools"],
            },
            {"name": "qwen3:14b", "size": 1, "details": {}, "capabilities": ["thinking"]},
        ]
    }
    facts, resource = declared_from_ollama_tags(payload, "hermes3:8b")
    by_name = {f.name: f.value for f in facts}
    assert by_name["context_window"] == "131072"
    assert by_name["parameter_size"] == "8.0B"
    assert by_name["quantization"] == "Q4_0"
    # Capability flags surfaced as true/false (so a diff catches a swap
    # that drops tool support — the granite case).
    assert by_name["tools"] == "true"
    assert by_name["thinking"] == "false"
    assert by_name["vision"] == "false"
    assert resource.declared_size_bytes == 4_661_227_243


def test_declared_from_ollama_tags_model_not_found() -> None:
    from gateway.capability_profile import declared_from_ollama_tags

    facts, resource = declared_from_ollama_tags({"models": []}, "nope:1b")
    assert facts == []
    assert resource.declared_size_bytes is None


# --------------------------------------------------------------- persistence


def _full_profile() -> CapabilityProfile:
    return CapabilityProfile(
        alias="fitt-ec2-hermes",
        model_id="hermes3:8b",
        captured_at=_ts(),
        declared=[DeclaredFact("context_window", "131072"), DeclaredFact("tools", "true")],
        measured=[
            MeasuredGrade("tool-calling", 0.9, 27, 30, 30, p50_latency_s=3.1, p95_latency_s=4.0),
            MeasuredGrade("coding", 0.7, 7, 10, 10, p50_latency_s=5.2, avg_in_tokens=8000.0),
        ],
        resource=ResourceUsage(declared_size_bytes=4_661_227_243),
    )


def test_profile_dict_round_trip() -> None:
    from gateway.capability_profile import profile_from_dict, profile_to_dict

    p = _full_profile()
    assert profile_from_dict(profile_to_dict(p)) == p


def test_write_and_load_baseline_round_trip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from gateway.capability_profile import load_baseline, write_profile

    p = _full_profile()
    md_path, json_path = write_profile(p, tmp_path)
    assert md_path.exists()
    assert json_path.exists()
    assert md_path.name == "fitt-ec2-hermes-profile.md"
    assert json_path.name == "fitt-ec2-hermes-profile.json"
    loaded = load_baseline("fitt-ec2-hermes", tmp_path)
    assert loaded == p


def test_load_baseline_missing_is_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from gateway.capability_profile import load_baseline

    assert load_baseline("never-profiled", tmp_path) is None


def test_round_trip_then_diff_is_clean(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A profile written and reloaded must diff clean against itself —
    serialization must not perturb values enough to fabricate a delta."""
    from gateway.capability_profile import load_baseline, write_profile

    p = _full_profile()
    write_profile(p, tmp_path)
    baseline = load_baseline("fitt-ec2-hermes", tmp_path)
    assert baseline is not None
    assert not p.diff(baseline).has_changes

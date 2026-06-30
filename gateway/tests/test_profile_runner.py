"""Tests for the extracted capability-profile producer (Phase 12.5a).

Two concerns:

* ``suite_grade`` folds a multi-sample eval suite's per-case results
  into one MeasuredGrade (the grade math shared by CLI + endpoint).
* ``run_profile`` assembles a CapabilityProfile with the expected
  measured dimensions from (faked) suite + election results — the
  glue, exercised without a live backend by patching the two heavy
  measurement calls.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from gateway.app import create_app
from gateway.planner import PlanElectionMulti, PlanElectionSample
from gateway.profile_runner import run_profile, suite_grade

from ._fixtures import build_test_config


def _case(passes: int, valid: int, total: int, latencies: list[float]) -> Any:
    return SimpleNamespace(
        passes=passes,
        valid=valid,
        total=total,
        samples=[SimpleNamespace(latency_ms=lat) for lat in latencies],
    )


def test_suite_grade_folds_cases() -> None:
    results = [_case(2, 2, 2, [100.0, 120.0]), _case(1, 2, 2, [80.0, 200.0])]
    grade = suite_grade("tool-calling", results)
    assert grade.name == "tool-calling"
    assert grade.passes == 3
    assert grade.valid == 4
    assert grade.samples == 4
    assert grade.pass_rate == 0.75


async def test_run_profile_assembles_three_dimensions(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)

    fake_tool = [_case(5, 5, 5, [100.0])]
    fake_coding = [_case(4, 5, 5, [200.0])]
    election = PlanElectionMulti(
        alias="fitt-default",
        samples=[
            PlanElectionSample(
                planned=True, transient=False, latency_ms=1000.0, in_tokens=50, out_tokens=20
            ),
            PlanElectionSample(
                planned=False, transient=False, latency_ms=900.0, in_tokens=40, out_tokens=10
            ),
        ],
    )

    with (
        patch(
            "gateway.alias_eval.run_eval_suite_multi",
            new=AsyncMock(side_effect=[fake_tool, fake_coding]),
        ),
        patch(
            "gateway.planner.measure_plan_election",
            new=AsyncMock(return_value=election),
        ),
        # fitt-default is an Ollama binding at a tailnet host; skip the
        # declared-facts ping (run_profile swallows the failure).
        patch("httpx.get", side_effect=RuntimeError("no network in test")),
    ):
        profile = await run_profile(
            alias="fitt-default", cfg=cfg, state=app.state, samples=2, timeout_s=5.0
        )

    assert profile.model_id == "qwen-big"
    assert [g.name for g in profile.measured] == ["tool-calling", "coding", "plan-election"]
    by_name = {g.name: g for g in profile.measured}
    assert by_name["tool-calling"].pass_rate == 1.0
    assert by_name["coding"].pass_rate == 0.8
    # 1 of 2 valid election samples planned -> 50%.
    assert by_name["plan-election"].pass_rate == 0.5
    # Declared ping was skipped, so no declared block — measured stands alone.
    assert profile.declared == []

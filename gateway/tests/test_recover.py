"""Tests for Phase 12 task 14 — recovery policy (pure decisions).

The escalation ladder and the honest-report builder are pure logic;
the orchestrator's wiring of them (re-runs, clean context) is covered
in test_orchestrator.py.
"""

from __future__ import annotations

from gateway.plan_store import Plan, PlanItem
from gateway.recover import MAX_RECOVERY_ATTEMPTS, decide_recovery, honest_report
from gateway.trouble import NO_TROUBLE, Trouble


def _t(kind: str) -> Trouble:
    return Trouble(kind, f"{kind} detail")  # type: ignore[arg-type]


# --------------------------------------------------------------- decide_recovery


def test_no_trouble_stops() -> None:
    assert decide_recovery(NO_TROUBLE, attempt=0, replanned=False) == "stop"


def test_cheap_signal_nudges_first() -> None:
    for kind in ("empty_after_tools", "tool_error", "identical_retry"):
        assert decide_recovery(_t(kind), attempt=0, replanned=False) == "nudge"


def test_zero_progress_replans_immediately() -> None:
    assert decide_recovery(_t("zero_progress"), attempt=0, replanned=False) == "replan"


def test_budget_exhausted_replans_immediately() -> None:
    assert decide_recovery(_t("budget_exhausted"), attempt=0, replanned=False) == "replan"


def test_nudge_then_replan_escalation() -> None:
    # A cheap signal that recurred after one nudge escalates to replan.
    assert decide_recovery(_t("tool_error"), attempt=1, replanned=False) == "replan"


def test_second_trouble_after_replan_stops() -> None:
    assert decide_recovery(_t("zero_progress"), attempt=1, replanned=True) == "stop"
    assert decide_recovery(_t("tool_error"), attempt=1, replanned=True) == "stop"


def test_attempt_cap_stops() -> None:
    assert decide_recovery(_t("tool_error"), attempt=MAX_RECOVERY_ATTEMPTS, replanned=False) == (
        "stop"
    )


# --------------------------------------------------------------- honest_report


def test_honest_report_names_observation() -> None:
    report = honest_report(_t("tool_error"), None)
    assert "tool_error detail" in report
    assert "stopping" in report.lower()


def test_honest_report_includes_plan_progress() -> None:
    plan = Plan(
        items=[
            PlanItem("1", "fetch data", "done"),
            PlanItem("2", "analyse", "pending"),
            PlanItem("3", "summarise", "pending"),
        ]
    )
    report = honest_report(_t("zero_progress"), plan)
    assert "1/3" in report
    assert "analyse" in report  # the next incomplete step


def test_honest_report_without_plan_omits_progress() -> None:
    report = honest_report(_t("budget_exhausted"), None)
    assert "Progress" not in report


def test_honest_report_complete_plan_has_no_next_step() -> None:
    plan = Plan(items=[PlanItem("1", "only", "done")])
    report = honest_report(_t("tool_error"), plan)
    assert "1/1" in report
    assert "next incomplete step" not in report.lower()

"""The tracked feature/model standing.

Two distinctions this pins, both learned by getting them wrong: a
scenario a model never ran must not read as a failure, and a run that
wasn't scored (unsupported feature / inconclusive) must not either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gateway.e2e_matrix import (
    CELL_FAIL,
    CELL_INCONCLUSIVE,
    CELL_MISSING,
    CELL_PASS,
    CELL_UNSUPPORTED,
    build_standing,
    latest_per_dut,
    load_sidecars,
    render_standing,
)


def _run(
    dut: str,
    scenarios: list[tuple[str, str, str]],
    *,
    ts: str = "2026-08-11T10:00:00+00:00",
    model: str | None = None,
    mode: str = "flat",
) -> dict[str, Any]:
    """scenarios: (name, status, objective)."""
    return {
        "schema": 1,
        "dut": dut,
        "model": model,
        "ts": ts,
        "samples": 1,
        "mode": mode,
        "judge_command": "kiro-cli chat --model claude-sonnet-4.5",
        "judge_model": "claude-sonnet-4.5",
        "objective_passed": sum(1 for _, st, o in scenarios if st == "scored" and o == "pass"),
        "total": sum(1 for _, st, _ in scenarios if st == "scored"),
        "scenarios": [
            {"scenario": n, "status": st, "objective": o, "judge": "unjudged", "reason": ""}
            for n, st, o in scenarios
        ],
    }


def test_pass_and_fail_cells() -> None:
    standing = build_standing(
        [_run("qwen3", [("todo", "scored", "pass"), ("x", "scored", "fail")])]
    )

    assert standing.cell("todo", "qwen3") == CELL_PASS
    assert standing.cell("x", "qwen3") == CELL_FAIL


def test_unscored_states_get_their_own_cells() -> None:
    standing = build_standing(
        [
            _run(
                "qwen3",
                [
                    ("cross_recall", "inconclusive", "fail"),
                    ("memory_recall", "unsupported", "fail"),
                ],
            )
        ]
    )

    assert standing.cell("cross_recall", "qwen3") == CELL_INCONCLUSIVE
    assert standing.cell("memory_recall", "qwen3") == CELL_UNSUPPORTED


def test_never_measured_is_not_a_failure() -> None:
    """Adding a scenario must not silently downgrade older models."""
    standing = build_standing(
        [
            _run("old", [("todo", "scored", "pass")]),
            _run("new", [("todo", "scored", "pass"), ("brand_new", "scored", "pass")]),
        ]
    )

    assert standing.cell("brand_new", "old") == CELL_MISSING
    assert standing.stale_duts() == ["old"]


def test_latest_run_per_dut_wins() -> None:
    older = _run("qwen3", [("todo", "scored", "fail")], ts="2026-08-01T10:00:00+00:00")
    newer = _run("qwen3", [("todo", "scored", "pass")], ts="2026-08-11T10:00:00+00:00")

    latest = latest_per_dut([older, newer])

    assert latest["qwen3"]["ts"] == newer["ts"]
    assert build_standing([older, newer]).cell("todo", "qwen3") == CELL_PASS


def test_render_includes_legend_and_provenance() -> None:
    rendered = render_standing(
        build_standing([_run("qwen3", [("todo", "scored", "pass")], model="qwen3:14b")])
    )

    assert "| todo |" in rendered
    assert "Legend" in rendered
    assert "qwen3:14b" in rendered
    assert "claude-sonnet-4.5" in rendered  # pinned judge is provenance


def test_render_flags_models_that_are_not_comparable() -> None:
    rendered = render_standing(
        build_standing(
            [
                _run("old", [("todo", "scored", "pass")]),
                _run("new", [("todo", "scored", "pass"), ("brand_new", "scored", "fail")]),
            ]
        )
    )

    assert "Not comparable like-for-like" in rendered
    assert "old" in rendered


def test_render_with_no_runs_says_what_to_do() -> None:
    rendered = render_standing(build_standing([]))

    assert "fitt eval e2e" in rendered


def test_load_sidecars_skips_foreign_json(tmp_path: Path) -> None:
    """The eval dir also holds alias-eval and profile sidecars."""
    (tmp_path / "e2e.json").write_text(
        json.dumps(_run("qwen3", [("todo", "scored", "pass")])), encoding="utf-8"
    )
    (tmp_path / "profile.json").write_text(json.dumps({"alias": "x"}), encoding="utf-8")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    runs = load_sidecars(tmp_path)

    assert [r["dut"] for r in runs] == ["qwen3"]


def test_load_sidecars_orders_oldest_first(tmp_path: Path) -> None:
    (tmp_path / "b.json").write_text(
        json.dumps(_run("m", [("t", "scored", "pass")], ts="2026-08-11T10:00:00+00:00")),
        encoding="utf-8",
    )
    (tmp_path / "a.json").write_text(
        json.dumps(_run("m", [("t", "scored", "fail")], ts="2026-08-01T10:00:00+00:00")),
        encoding="utf-8",
    )

    runs = load_sidecars(tmp_path)

    assert [r["ts"] for r in runs] == [
        "2026-08-01T10:00:00+00:00",
        "2026-08-11T10:00:00+00:00",
    ]


def test_missing_eval_dir_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_sidecars(tmp_path / "nope") == []


# ------------------------------------------- judge provenance
#
# The judge model is part of the measurement: switching it changes the
# quality scores, so the standing view has to say which model produced
# which verdict rather than leaving it implicit in a shell string.


def test_judge_model_is_extracted_from_the_command() -> None:
    from gateway.e2e_eval import judge_model_from_command

    assert (
        judge_model_from_command("kiro-cli chat --no-interactive --model claude-sonnet-5")
        == "claude-sonnet-5"
    )
    assert judge_model_from_command("kiro-cli chat --model=claude-haiku-4.5") == "claude-haiku-4.5"


def test_auto_and_missing_model_are_recorded_as_unpinned() -> None:
    """An unpinned judge's default moves between runs, so it can't back a
    comparison — the sidecar must say so rather than look specific."""
    from gateway.e2e_eval import UNPINNED_JUDGE, judge_model_from_command

    assert judge_model_from_command("kiro-cli chat --model auto") == UNPINNED_JUDGE
    assert judge_model_from_command("kiro-cli chat --no-interactive") == UNPINNED_JUDGE


def test_no_judge_command_means_no_judge_model() -> None:
    from gateway.e2e_eval import judge_model_from_command

    assert judge_model_from_command(None) is None
    assert judge_model_from_command("") is None


def test_render_warns_when_judges_differ_between_models() -> None:
    old = _run("qwen3", [("todo", "scored", "pass")])
    old["judge_model"] = "claude-sonnet-4.5"
    new = _run("gemma4", [("todo", "scored", "pass")])
    new["judge_model"] = "claude-sonnet-5"

    rendered = render_standing(build_standing([old, new]))

    assert "more than one model" in rendered
    assert "claude-sonnet-4.5" in rendered
    assert "claude-sonnet-5" in rendered
    # Objective results survive a judge change; say so.
    assert "Objective results are unaffected" in rendered


def test_render_flags_an_unpinned_judge() -> None:
    run = _run("qwen3", [("todo", "scored", "pass")])
    run["judge_model"] = "unpinned"

    rendered = render_standing(build_standing([run]))

    assert "unpinned judge" in rendered


# ------------------------------------------- loop mode as a column axis
#
# A model on the plan->execute orchestrator is a different subject from the
# same model on the flat loop. Keyed on DUT alone, the planned run silently
# overwrote the flat one — destroying the comparison it was run for.


def test_flat_and_planned_runs_get_separate_columns() -> None:
    flat = _run("gemma4", [("multi_step_chain", "scored", "fail")], mode="flat")
    planned = _run(
        "gemma4",
        [("multi_step_chain", "scored", "pass")],
        mode="planned",
        ts="2026-08-14T10:00:00+00:00",
    )

    standing = build_standing([flat, planned])

    assert standing.duts == ["gemma4", "gemma4 [planned]"]
    assert standing.cell("multi_step_chain", "gemma4") == CELL_FAIL
    assert standing.cell("multi_step_chain", "gemma4 [planned]") == CELL_PASS


def test_a_newer_flat_run_replaces_an_unrecorded_one() -> None:
    """Pre-`--mode` runs were flat in practice, so a pinned flat run should
    supersede them rather than sit in its own column forever."""
    old = _run("gemma4", [("todo", "scored", "fail")], ts="2026-08-01T10:00:00+00:00")
    old["mode"] = "unrecorded"
    new = _run("gemma4", [("todo", "scored", "pass")], ts="2026-08-14T10:00:00+00:00", mode="flat")

    standing = build_standing([old, new])

    assert standing.duts == ["gemma4"]
    assert standing.cell("todo", "gemma4") == CELL_PASS


def test_provenance_names_the_loop() -> None:
    rendered = render_standing(
        build_standing([_run("gemma4", [("todo", "scored", "pass")], mode="planned")])
    )

    assert "loop=planned" in rendered


def test_unrecorded_loop_is_called_out_as_uninterpretable() -> None:
    run = _run("gemma4", [("todo", "scored", "pass")])
    run["mode"] = "unrecorded"

    rendered = render_standing(build_standing([run]))

    assert "loop=unrecorded" in rendered
    assert "nothing established it" in rendered


def test_a_pinned_run_does_not_trigger_the_unrecorded_warning() -> None:
    rendered = render_standing(
        build_standing([_run("gemma4", [("todo", "scored", "pass")], mode="flat")])
    )

    assert "nothing established it" not in rendered


def test_latest_per_dut_keys_on_mode_too() -> None:
    flat = _run("m", [("t", "scored", "pass")], mode="flat")
    planned = _run("m", [("t", "scored", "pass")], mode="planned")

    assert sorted(latest_per_dut([flat, planned])) == ["m", "m [planned]"]

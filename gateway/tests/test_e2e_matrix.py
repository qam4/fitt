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
) -> dict[str, Any]:
    """scenarios: (name, status, objective)."""
    return {
        "schema": 1,
        "dut": dut,
        "model": model,
        "ts": ts,
        "samples": 1,
        "judge_command": "kiro-cli chat --model claude-sonnet-4.5",
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

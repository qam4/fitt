"""Judged-e2e driver — snapshot + assertion helpers (Phase C task 7)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from gateway.app import create_app
from gateway.e2e_driver import cron_at_ts_matches, snapshot_app, todos_contain

from ._fixtures import build_test_config


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    return create_app(cfg)


def test_snapshot_reads_cron_and_empty_todos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path, monkeypatch)
    from gateway.cron import CronJob, parse_schedule_spec

    sched = parse_schedule_spec("at 2026-07-03T09:00:00Z")
    app.state.cron.add(
        CronJob(
            id="",
            name="reminder",
            message="call the doctor",
            schedule=sched,
            session_key="main",
            created_by_client="cli",
        )
    )

    snap = snapshot_app(app)
    jobs = snap["cron_jobs"]
    assert any(j["schedule_kind"] == "at" and j["at_ts"] is not None for j in jobs)
    assert snap["todos_text"] == ""  # no todos.md yet (Phase E)
    assert "event_kinds" in snap


def test_cron_at_ts_matches_helper() -> None:
    target = datetime.fromisoformat("2026-07-03T09:00:00+00:00").timestamp()
    snap = {"cron_jobs": [{"schedule_kind": "at", "at_ts": target}]}
    assert cron_at_ts_matches(snap, target, tolerance_s=900)
    assert cron_at_ts_matches(snap, target + 600, tolerance_s=900)  # within tolerance
    assert not cron_at_ts_matches(snap, target + 100_000, tolerance_s=900)
    # an interval cron doesn't count as a one-shot reminder
    assert not cron_at_ts_matches(
        {"cron_jobs": [{"schedule_kind": "every", "at_ts": None}]}, target
    )


def test_todos_contain_helper() -> None:
    assert todos_contain({"todos_text": "- Call the Doctor\n"}, "call the doctor")
    assert not todos_contain({"todos_text": ""}, "anything")
    assert not todos_contain({}, "anything")

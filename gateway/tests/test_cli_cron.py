"""Tests for ``fitt cron`` CLI — Phase 4.5 Task 8.

The CLI opens the CronService file at ``$FITT_HOME/cron.json``
directly and mutates it; the running gateway picks up the
changes via mtime-based reload. We verify the CLI commands
round-trip through the CronService rather than testing Click's
arg parsing in isolation.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gateway.cli import main as fitt_cli
from gateway.cron import CronService, default_cron_path


def _open_service(fitt_home: Path) -> CronService:
    return CronService(default_cron_path(fitt_home))


def test_cron_list_empty(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["cron", "list"])
    assert result.exit_code == 0, result.output
    assert "No crons" in result.output


def test_cron_add_creates_job(isolate_fitt_home: Path) -> None:
    """``fitt cron add`` writes a job the CronService can read
    back. The scheduler + mtime-based reload would pick it up
    on the next tick; here we just verify persistence."""
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        [
            "cron",
            "add",
            "--name",
            "briefing",
            "--schedule",
            "every 5m",
            "--message",
            "summarise my open PRs",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created" in result.output
    assert "briefing" in result.output

    jobs = _open_service(isolate_fitt_home).list()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.name == "briefing"
    assert j.schedule.kind == "every"
    assert j.schedule.every_secs == 300
    assert j.message == "summarise my open PRs"
    assert j.enabled is True
    assert j.silent is False
    assert j.approval_mode == ""
    assert j.agent_alias == ""
    assert j.created_by_client == "cli"


def test_cron_add_invalid_schedule_exits_nonzero(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        [
            "cron",
            "add",
            "--name",
            "bad",
            "--schedule",
            "gibberish",
            "--message",
            "x",
        ],
    )
    assert result.exit_code == 1
    assert "invalid schedule" in result.output.lower()


def test_cron_add_flags_silent_and_auto_approve(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        [
            "cron",
            "add",
            "--name",
            "monitor",
            "--schedule",
            "every 60s",
            "--message",
            "poll the build",
            "--silent",
            "--auto-approve",
            "--alias",
            "fitt-smart",
        ],
    )
    assert result.exit_code == 0, result.output
    jobs = _open_service(isolate_fitt_home).list()
    assert len(jobs) == 1
    j = jobs[0]
    assert j.silent is True
    assert j.approval_mode == "auto"
    assert j.agent_alias == "fitt-smart"


def test_cron_list_shows_active_job(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(
        fitt_cli,
        [
            "cron",
            "add",
            "--name",
            "briefing",
            "--schedule",
            "every 10m",
            "--message",
            "x",
        ],
    )
    result = runner.invoke(fitt_cli, ["cron", "list"])
    assert result.exit_code == 0, result.output
    assert "briefing" in result.output
    assert "every 10m" in result.output
    assert "active" in result.output


def test_cron_pause_and_resume(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    add = runner.invoke(
        fitt_cli,
        ["cron", "add", "--name", "n", "--schedule", "every 60s", "--message", "m"],
    )
    # Pull the id out of the add output.
    svc = _open_service(isolate_fitt_home)
    job_id = svc.list()[0].id
    assert add.exit_code == 0

    pause = runner.invoke(fitt_cli, ["cron", "pause", job_id])
    assert pause.exit_code == 0, pause.output
    assert _open_service(isolate_fitt_home).get(job_id).enabled is False  # type: ignore[union-attr]

    # Default list hides paused; --all shows them.
    default = runner.invoke(fitt_cli, ["cron", "list"])
    assert job_id not in default.output
    all_ = runner.invoke(fitt_cli, ["cron", "list", "--all"])
    assert job_id in all_.output
    assert "paused" in all_.output

    resume = runner.invoke(fitt_cli, ["cron", "resume", job_id])
    assert resume.exit_code == 0, resume.output
    assert _open_service(isolate_fitt_home).get(job_id).enabled is True  # type: ignore[union-attr]


def test_cron_pause_unknown_exits_nonzero(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["cron", "pause", "deadbeef"])
    assert result.exit_code == 1
    assert "deadbeef" in result.output


def test_cron_remove(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(
        fitt_cli,
        ["cron", "add", "--name", "doomed", "--schedule", "every 60s", "--message", "m"],
    )
    job_id = _open_service(isolate_fitt_home).list()[0].id

    remove = runner.invoke(fitt_cli, ["cron", "remove", job_id])
    assert remove.exit_code == 0, remove.output
    assert _open_service(isolate_fitt_home).get(job_id) is None

    # Remove unknown → non-zero.
    second = runner.invoke(fitt_cli, ["cron", "remove", job_id])
    assert second.exit_code == 1

"""Tests for ``fitt learn`` CLI — Phase 5.

Mirrors the ``fitt cron`` CLI tests. The CLI opens the lessons
file at ``$FITT_HOME/identity/lessons.md`` directly; the
running gateway picks up changes via mtime-based reload.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from gateway.cli import main as fitt_cli
from gateway.lessons import LessonsStore, default_lessons_path


def _open_store(fitt_home: Path) -> LessonsStore:
    identity_dir = fitt_home / "identity"
    identity_dir.mkdir(exist_ok=True)
    return LessonsStore(default_lessons_path(identity_dir))


def test_learn_list_empty(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["learn", "list"])
    assert result.exit_code == 0, result.output
    assert "No lessons" in result.output


def test_learn_add_persists(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["learn", "add", "always use uv, not pip"])
    assert result.exit_code == 0, result.output
    assert "Recorded" in result.output
    lessons = _open_store(isolate_fitt_home).read()
    assert len(lessons) == 1
    assert lessons[0].text == "always use uv, not pip"
    assert lessons[0].category is None


def test_learn_add_with_category(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["learn", "add", "prefer ruff format", "--category", "tooling"],
    )
    assert result.exit_code == 0, result.output
    lessons = _open_store(isolate_fitt_home).read()
    assert lessons[0].category == "tooling"


def test_learn_add_empty_text_exits_nonzero(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["learn", "add", "   "])
    assert result.exit_code == 1
    assert "non-empty" in result.output.lower()


def test_learn_list_shows_recorded_lessons(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(fitt_cli, ["learn", "add", "always use uv"])
    runner.invoke(
        fitt_cli,
        ["learn", "add", "prefer ruff", "--category", "tooling"],
    )
    result = runner.invoke(fitt_cli, ["learn", "list"])
    assert result.exit_code == 0, result.output
    assert "always use uv" in result.output
    assert "prefer ruff" in result.output
    assert "tooling" in result.output


def test_learn_remove_match(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(fitt_cli, ["learn", "add", "always use uv"])
    runner.invoke(fitt_cli, ["learn", "add", "UV runs on Windows"])
    runner.invoke(fitt_cli, ["learn", "add", "prefer ruff"])
    result = runner.invoke(fitt_cli, ["learn", "remove", "uv"])
    assert result.exit_code == 0, result.output
    assert "Removed 2" in result.output
    remaining = [lsn.text for lsn in _open_store(isolate_fitt_home).read()]
    assert remaining == ["prefer ruff"]


def test_learn_remove_no_match(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    runner.invoke(fitt_cli, ["learn", "add", "always use uv"])
    result = runner.invoke(fitt_cli, ["learn", "remove", "nonexistent"])
    assert result.exit_code == 0, result.output
    assert "No lessons matched" in result.output
    assert len(_open_store(isolate_fitt_home).read()) == 1


def test_learn_path_prints_file_path(isolate_fitt_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["learn", "path"])
    assert result.exit_code == 0, result.output
    assert "lessons.md" in result.output

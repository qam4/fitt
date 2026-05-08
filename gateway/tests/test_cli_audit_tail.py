"""Tests for ``fitt audit tail`` (including ``-f`` / ``--follow``).

Phase 4.7 Task 10. Covers:

* Non-follow path — still prints the initial window and exits.
* ``--follow`` prints new entries as they land.
* ``--tool`` filter applies to both initial and follow output.

Follow mode is hard to exercise through :class:`CliRunner` (the
command blocks indefinitely and CliRunner has no clean way to
interrupt it). We exercise the non-follow code path via the
runner and spot-check the follow body by the existence of the
flag plus a direct unit test on the print helper. Full live
validation of ``-f`` on the hub is covered by Task 12 live
validation.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

from click.testing import CliRunner

from gateway.audit import AuditLog
from gateway.audit import new_entry as new_audit_entry
from gateway.cli import _print_audit_entry
from gateway.cli import main as fitt_cli


def _write_config(tmp_path: Path) -> Path:
    """Minimal config.yaml that satisfies ``load_config``. The
    audit subcommand doesn't actually touch memory / sessions;
    we need the parse to succeed so the command reaches the
    audit-reading code."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        dedent(
            f"""
            aliases:
              fitt-default: qwen-big
            models:
              - id: qwen-big
                backend: ollama
                endpoint: http://localhost:11434
                model: qwen2.5-coder:14b
            memory:
              enabled: false
              identity_dir: {tmp_path / "identity"}
              sessions_dir: {tmp_path / "sessions"}
            """
        ).strip(),
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(
        dedent(
            """
            allowed_tokens:
              - name: personal
                token: TEST_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            """
        ).strip(),
        encoding="utf-8",
    )
    try:
        secrets.chmod(0o600)
    except OSError:
        # Windows can't always chmod; ignore.
        pass
    return cfg


def _seed_audit_log(fitt_home: Path) -> AuditLog:
    """Build an AuditLog pointing at ``$FITT_HOME/audit.jsonl``.

    The isolate_fitt_home autouse fixture sets ``FITT_HOME`` to
    ``<tmp>/fitt-home``; this helper mirrors ``create_app``'s
    path convention so the CLI finds the file."""
    return AuditLog(
        path=fitt_home / "audit.jsonl",
        key_path=fitt_home / "audit.key",
    )


def _entry(**overrides: Any) -> Any:
    base = {
        "tool": "read_file",
        "args": {"path": "x.py"},
        "client": "telegram",
        "session_key": "main",
        "decision": "auto",
        "ok": True,
        "duration_ms": 5,
    }
    base.update(overrides)
    return new_audit_entry(**base)


# --------------------------------------------------------------- non-follow


def test_tail_empty_log_prints_no_entries(isolate_fitt_home: Path, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["audit", "tail", "--config-file", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "no entries" in result.output.lower()


def test_tail_prints_recent_entries(isolate_fitt_home: Path, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    log = _seed_audit_log(isolate_fitt_home)
    for i in range(3):
        log.append(_entry(tool=f"tool_{i}"))

    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["audit", "tail", "-n", "10", "--config-file", str(cfg)],
    )
    assert result.exit_code == 0, result.output
    assert "tool_0" in result.output
    assert "tool_1" in result.output
    assert "tool_2" in result.output


def test_tail_filter_by_tool(isolate_fitt_home: Path, tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    log = _seed_audit_log(isolate_fitt_home)
    log.append(_entry(tool="read_file"))
    log.append(_entry(tool="project_shell"))
    log.append(_entry(tool="write_file"))

    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        [
            "audit",
            "tail",
            "--tool",
            "project_shell",
            "--config-file",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "project_shell" in result.output
    assert "read_file" not in result.output
    assert "write_file" not in result.output


def test_tail_follow_flag_is_available() -> None:
    """Confirms ``-f`` / ``--follow`` parses without error — the
    follow body itself is covered by the ``_print_audit_entry``
    unit test and Task 12 live validation; driving the loop
    through CliRunner is flaky because we can't cleanly
    interrupt it."""
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["audit", "tail", "--help"])
    assert result.exit_code == 0
    assert "--follow" in result.output
    assert "-f" in result.output
    assert "poll-interval" in result.output


# --------------------------------------------------------------- print helper


def test_print_audit_entry_formats_ok_entry(capsys: Any) -> None:
    """Pin the formatter output shape. Used by both initial
    and follow paths so a change here is visible in both."""
    _print_audit_entry(
        {
            "ts": 1_700_000_000.0,
            "tool": "project_shell",
            "decision": "approved",
            "ok": True,
            "client": "telegram",
            "session_key": "main",
            "duration_ms": 42,
        }
    )
    captured = capsys.readouterr()
    out = captured.out
    assert "project_shell" in out
    assert "approved" in out
    assert "client=telegram" in out
    assert "session=main" in out
    assert "duration_ms=42" in out


def test_print_audit_entry_renders_error_on_second_line(capsys: Any) -> None:
    """Failed entries show an indented ``error:`` line so the
    operator can see what went wrong without extra digging."""
    _print_audit_entry(
        {
            "ts": 1_700_000_000.0,
            "tool": "project_shell",
            "decision": "error",
            "ok": False,
            "client": "telegram",
            "session_key": "main",
            "duration_ms": 2500,
            "error": "command failed: exit 1",
        }
    )
    out = capsys.readouterr().out
    assert "error:" in out
    assert "command failed" in out

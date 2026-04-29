"""Tests for the `fitt` CLI."""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

from click.testing import CliRunner

from gateway.cli import main as fitt_cli


def _write_log(log_dir: Path, lines: list[dict]) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "gateway.log"
    with path.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def test_cli_cost_aggregates_from_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write_log(
        log_dir,
        [
            {
                "timestamp": "2026-04-29T10:00:00Z",
                "event": "chat.completion",
                "model": "anthropic/claude-sonnet-4.5",
                "input_tokens": 1000,
                "output_tokens": 500,
                "cost_usd": "0.0105",
            },
            {
                "timestamp": "2026-04-29T11:00:00Z",
                "event": "chat.completion",
                "model": "anthropic/claude-sonnet-4.5",
                "input_tokens": 2000,
                "output_tokens": 500,
                "cost_usd": "0.0135",
            },
            {
                "timestamp": "2026-04-29T12:00:00Z",
                "event": "chat.completion",
                "model": "qwen2.5-coder:14b",
                "input_tokens": 500,
                "output_tokens": 300,
                "cost_usd": "0",
            },
            # Not in April → filtered out
            {
                "timestamp": "2026-03-15T09:00:00Z",
                "event": "chat.completion",
                "model": "anthropic/claude-sonnet-4.5",
                "input_tokens": 9999,
                "output_tokens": 9999,
                "cost_usd": "99.99",
            },
            # Not a chat event → ignored
            {
                "timestamp": "2026-04-29T12:00:00Z",
                "event": "gateway.starting",
            },
        ],
    )

    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["cost", "--log-dir", str(log_dir), "--month", "2026-04"])
    assert result.exit_code == 0, result.output
    assert "anthropic/claude-sonnet-4.5" in result.output
    assert "qwen2.5-coder:14b" in result.output
    # Totals: 0.0105 + 0.0135 = 0.0240
    assert "0.0240" in result.output
    # March entry must be excluded
    assert "99.99" not in result.output


def test_cli_cost_no_logs(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["cost", "--log-dir", str(tmp_path / "nope")])
    assert result.exit_code == 0
    assert "No log dir" in result.output


def test_cli_config_check_valid(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        dedent(
            """
            aliases:
              fitt-default: qwen-big
            models:
              - id: qwen-big
                backend: ollama
                endpoint: http://localhost:11434
                model: qwen2.5-coder:14b
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
                token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            """
        ).strip(),
        encoding="utf-8",
    )
    import os as _os

    if _os.name != "nt":
        secrets.chmod(0o600)

    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["config", "check", "--config-file", str(cfg), "--secrets-file", str(secrets)],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "fitt-default" in result.output


def test_cli_config_check_rejects_bad_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        dedent(
            """
            aliases:
              fitt-default: does-not-exist
            models:
              - id: something-else
                backend: ollama
                endpoint: http://localhost:11434
                model: qwen2.5-coder:14b
            """
        ).strip(),
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(
        "allowed_tokens:\n  - name: personal\n    token: AAAAAAAAAAAAAAAAAAAAAAAAAA\n",
        encoding="utf-8",
    )
    import os as _os

    if _os.name != "nt":
        secrets.chmod(0o600)

    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["config", "check", "--config-file", str(cfg), "--secrets-file", str(secrets)],
    )
    assert result.exit_code == 1
    assert "Configuration invalid" in result.output

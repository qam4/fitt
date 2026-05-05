"""Tests for the `fitt` CLI."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from textwrap import dedent

import pytest
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


# --------------------------------------------------------------- fitt session


def _write_session_config(tmp_path: Path) -> Path:
    """Write a config.yaml that points memory/sessions at tmp_path."""
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
              enabled: true
              identity_dir: {tmp_path / "identity"}
              sessions_dir: {tmp_path / "sessions"}
            """
        ).strip(),
        encoding="utf-8",
    )
    return cfg


def test_cli_session_list_shows_main(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["session", "list", "--config-file", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "main" in result.output


def test_cli_session_new_valid(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["session", "new", "retroai", "--name", "Retro AI", "--config-file", str(cfg)],
    )
    assert result.exit_code == 0, result.output
    assert "Created" in result.output

    # list should now show both
    list_result = runner.invoke(fitt_cli, ["session", "list", "--config-file", str(cfg)])
    assert "retroai" in list_result.output


def test_cli_session_new_invalid_id(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["session", "new", "BAD-ID", "--config-file", str(cfg)])
    assert result.exit_code == 1
    assert "invalid" in result.output.lower()


def test_cli_session_new_duplicate(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    runner.invoke(fitt_cli, ["session", "new", "foo", "--config-file", str(cfg)])
    dup = runner.invoke(fitt_cli, ["session", "new", "foo", "--config-file", str(cfg)])
    assert dup.exit_code == 1
    assert "already" in dup.output.lower()


def test_cli_session_rename(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    runner.invoke(fitt_cli, ["session", "new", "foo", "--config-file", str(cfg)])
    result = runner.invoke(
        fitt_cli,
        ["session", "rename", "foo", "--name", "Foo Bar", "--config-file", str(cfg)],
    )
    assert result.exit_code == 0
    list_result = runner.invoke(fitt_cli, ["session", "list", "--config-file", str(cfg)])
    assert "Foo Bar" in list_result.output


def test_cli_session_rename_main_refused(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["session", "rename", "main", "--name", "nope", "--config-file", str(cfg)],
    )
    assert result.exit_code == 1


def test_cli_session_archive_and_unarchive(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    runner.invoke(fitt_cli, ["session", "new", "foo", "--config-file", str(cfg)])

    arch = runner.invoke(fitt_cli, ["session", "archive", "foo", "--config-file", str(cfg)])
    assert arch.exit_code == 0

    # list without --include-archived should not show foo
    plain = runner.invoke(fitt_cli, ["session", "list", "--config-file", str(cfg)])
    assert "foo" not in plain.output

    # --include-archived should show it
    with_arch = runner.invoke(
        fitt_cli,
        ["session", "list", "--include-archived", "--config-file", str(cfg)],
    )
    assert "foo" in with_arch.output

    # unarchive restores
    runner.invoke(fitt_cli, ["session", "unarchive", "foo", "--config-file", str(cfg)])
    plain2 = runner.invoke(fitt_cli, ["session", "list", "--config-file", str(cfg)])
    assert "foo" in plain2.output


def test_cli_session_archive_main_refused(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["session", "archive", "main", "--config-file", str(cfg)])
    assert result.exit_code == 1


def test_cli_session_path(tmp_path: Path) -> None:
    cfg = _write_session_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["session", "path", "main", "--config-file", str(cfg)])
    assert result.exit_code == 0
    assert "main" in result.output
    assert "history" in result.output


# --------------------------------------------------------------- fitt ssh

_SSH_KEYGEN_AVAILABLE = shutil.which("ssh-keygen") is not None


@pytest.mark.skipif(
    not _SSH_KEYGEN_AVAILABLE,
    reason="ssh-keygen not on PATH; gateway runtime image installs it",
)
def test_cli_ssh_pubkey_generates_then_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fitt ssh pubkey` generates the key pair if missing and
    prints the public line. Running a second time prints the same
    line without regenerating."""
    key_path = tmp_path / "ssh" / "id_ed25519"
    monkeypatch.setenv("FITT_SSH_KEY_PATH", str(key_path))

    runner = CliRunner()

    first = runner.invoke(fitt_cli, ["ssh", "pubkey"])
    assert first.exit_code == 0, first.output
    assert first.output.strip().startswith("ssh-ed25519 ")

    # Second run doesn't regenerate.
    pub_bytes_first = (tmp_path / "ssh" / "id_ed25519.pub").read_bytes()
    second = runner.invoke(fitt_cli, ["ssh", "pubkey"])
    assert second.exit_code == 0
    assert second.output.strip() == first.output.strip()
    assert (tmp_path / "ssh" / "id_ed25519.pub").read_bytes() == pub_bytes_first


@pytest.mark.skipif(
    not _SSH_KEYGEN_AVAILABLE,
    reason="ssh-keygen not on PATH; gateway runtime image installs it",
)
def test_cli_ssh_test_reports_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`fitt ssh test` surfaces a human-friendly error when the
    host is unreachable. We deliberately target an RFC5737
    TEST-NET-1 address that should never respond."""
    key_path = tmp_path / "ssh" / "id_ed25519"
    monkeypatch.setenv("FITT_SSH_KEY_PATH", str(key_path))

    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["ssh", "test", "user@192.0.2.1", "--timeout", "1"],
    )
    assert result.exit_code == 1
    assert "ok" not in result.output.lower()

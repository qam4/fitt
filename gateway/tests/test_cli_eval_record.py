"""Tests for `fitt eval alias --record` (Phase 12 task 3 wiring).

The record/replay mechanism itself is covered in
``test_record_replay.py``; this pins the CLI glue: the flag wraps the
real router in a RecordingRouter, runs the suite against a stubbed
backend, and writes a cassette that ReplayRouter can load.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from click.testing import CliRunner

from gateway.cli import main as fitt_cli
from gateway.record_replay import Cassette, ReplayRouter

from ._llm_stubs import make_response


def _write_config_and_secrets(home: Path) -> None:
    (home / "config.yaml").write_text(
        dedent(
            """
            aliases:
              fitt-test: m1
            models:
              - id: m1
                backend: ollama
                endpoint: http://localhost:11434
                model: qwen3:8b
            """
        ).strip(),
        encoding="utf-8",
    )
    secrets = home / "secrets.yaml"
    secrets.write_text(
        dedent(
            """
            allowed_tokens:
              - name: test
                token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            """
        ).strip(),
        encoding="utf-8",
    )
    if os.name != "nt":
        secrets.chmod(0o600)


def test_eval_alias_record_writes_replayable_cassette(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config_and_secrets(isolate_fitt_home)

    async def _always(**_: Any) -> Any:
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", _always)

    cassette = tmp_path / "fitt-test.cassette.json"
    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["eval", "alias", "fitt-test", "--record", str(cassette), "--timeout", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "recorded" in result.output
    assert cassette.exists()

    # The cassette loads and carries at least one recorded dispatch,
    # and is replayable.
    cass = Cassette.load(cassette)
    assert len(cass.interactions) >= 1
    ReplayRouter(cass)  # constructs without error


def test_eval_alias_without_record_writes_no_cassette(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config_and_secrets(isolate_fitt_home)

    async def _always(**_: Any) -> Any:
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", _always)

    runner = CliRunner()
    result = runner.invoke(
        fitt_cli,
        ["eval", "alias", "fitt-test", "--timeout", "5"],
    )
    assert result.exit_code == 0, result.output
    assert "recorded" not in result.output

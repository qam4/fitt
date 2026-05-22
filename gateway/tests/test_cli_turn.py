"""Tests for ``fitt turn list`` / ``fitt turn show`` (Slice 7.2).

The CLI hits the running gateway over HTTP. Tests mock the
HTTP responses with respx and verify the CLI renders cleanly.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
import respx
from click.testing import CliRunner

from gateway.cli import main as fitt_cli


def _write_min_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "\n".join(
            [
                "aliases:",
                "  fitt-default: qwen-big",
                "models:",
                "  - id: qwen-big",
                "    backend: ollama",
                "    endpoint: http://localhost:11434",
                "    model: qwen2.5:14b",
                "logging:",
                "  dir: " + str(tmp_path / "logs"),
                "  retention_days: 7",
            ]
        ),
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(
        "\n".join(
            [
                "allowed_tokens:",
                "  - name: personal",
                "    token: TEST_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            ]
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        secrets.chmod(0o600)
    return cfg


@pytest.fixture(autouse=True)
def _config_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _write_min_config(tmp_path)
    monkeypatch.setenv("FITT_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("FITT_SECRETS_PATH", str(tmp_path / "secrets.yaml"))


@pytest.fixture(autouse=True)
def _gateway_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = "http://127.0.0.1:8421"
    monkeypatch.setenv("FITT_GATEWAY_URL", url)
    return url


# --------------------------------------------------------------- list


def test_turn_list_renders_summary_table() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/sessions/main/captures").mock(
            return_value=httpx.Response(
                200,
                json={
                    "session_key": "main",
                    "captures": [
                        {
                            "turn_id": "abc12345-6789-...",
                            "started_at": 1779479823.42,
                            "alias": "fitt-default",
                            "model_used": "qwen2.5-coder:14b",
                            "prompt_tokens": 5400,
                            "context_window": 32768,
                            "prompt_pct_of_window": 16.5,
                            "finish_reason": "stop",
                            "narration_warning": False,
                            "tool_calls_count": 1,
                            "status": "ok",
                        },
                    ],
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "list"])
    assert result.exit_code == 0, result.output
    assert "abc12345" in result.output
    assert "qwen2.5-coder:14b" in result.output
    assert "5,400" in result.output
    # 16.5 rounds via banker's rounding (the f-string ``:.0f``
    # uses ROUND_HALF_EVEN). Just check for the percent sign and
    # a 16 nearby.
    assert "16%" in result.output or "17%" in result.output
    assert "ok" in result.output


def test_turn_list_handles_empty_session() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/sessions/main/captures").mock(
            return_value=httpx.Response(
                200,
                json={"session_key": "main", "captures": []},
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "list"])
    assert result.exit_code == 0, result.output
    assert "No captured turns" in result.output


def test_turn_list_flags_narration_warning() -> None:
    """A captured turn with the narration warning flag should
    show the warning glyph in the Tools column so operators can
    spot it during a scan."""
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/sessions/main/captures").mock(
            return_value=httpx.Response(
                200,
                json={
                    "session_key": "main",
                    "captures": [
                        {
                            "turn_id": "warn-1",
                            "started_at": 1779479823.42,
                            "alias": "fitt-default",
                            "model_used": "granite3.3:8b",
                            "prompt_tokens": 5400,
                            "context_window": 32768,
                            "prompt_pct_of_window": 16.5,
                            "finish_reason": "stop",
                            "narration_warning": True,
                            "tool_calls_count": 0,
                            "status": "ok",
                        },
                    ],
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "list"])
    assert result.exit_code == 0, result.output
    assert "⚠" in result.output


def test_turn_list_passes_limit_query_param() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(
            "http://127.0.0.1:8421/v1/sessions/main/captures",
        ).mock(
            return_value=httpx.Response(200, json={"session_key": "main", "captures": []}),
        )
        runner.invoke(fitt_cli, ["turn", "list", "-n", "5"])
    assert route.called
    request = route.calls.last.request
    assert request.url.params.get("limit") == "5"


def test_turn_list_handles_transport_error() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/sessions/main/captures").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        result = runner.invoke(fitt_cli, ["turn", "list"])
    assert result.exit_code == 1
    assert "Could not reach gateway" in result.output


# --------------------------------------------------------------- show


def test_turn_show_renders_full_capture() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            "http://127.0.0.1:8421/v1/sessions/main/captures/turn-detail",
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "turn_id": "turn-detail",
                    "alias": "fitt-default",
                    "client": "telegram",
                    "model_used": "qwen2.5-coder:14b",
                    "backend": "ollama",
                    "fallback_used": False,
                    "started_at": 1779479823.42,
                    "finished_at": 1779479825.81,
                    "dispatched_messages": [
                        {"role": "system", "content": "[Capabilities]..."},
                        {"role": "user", "content": "Read README.md"},
                    ],
                    "response": {
                        "choices": [
                            {
                                "message": {"content": "OK, the readme starts with..."},
                                "finish_reason": "stop",
                            }
                        ]
                    },
                    "tool_calls": [
                        {
                            "call_id": "c1",
                            "tool_name": "read_file",
                            "args": {"path": "README.md"},
                            "decision": "auto",
                            "decision_detail": "",
                            "duration_ms": 12,
                            "ok": True,
                            "result_summary": "(file content)",
                            "artifact_path": None,
                            "iteration": 0,
                        }
                    ],
                    "prompt_tokens": 5400,
                    "completion_tokens": 89,
                    "context_window": 32768,
                    "prompt_pct_of_window": 16.5,
                    "finish_reason": "stop",
                    "narration_warning": False,
                    "iterations": 1,
                    "status": "ok",
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "show", "turn-detail"])
    assert result.exit_code == 0, result.output
    assert "turn-detail" in result.output
    assert "qwen2.5-coder:14b" in result.output
    assert "5,400" in result.output
    assert "16.5%" in result.output
    assert "read_file" in result.output
    assert "[user]" in result.output
    assert "Read README.md" in result.output
    assert "OK, the readme" in result.output


def test_turn_show_handles_404() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            "http://127.0.0.1:8421/v1/sessions/main/captures/missing",
        ).mock(
            return_value=httpx.Response(
                404,
                json={
                    "detail": {
                        "error": {
                            "type": "not_found",
                            "message": "capture for turn 'missing' not found...",
                        }
                    }
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "show", "missing"])
    assert result.exit_code == 1
    assert "Capture not found" in result.output


def test_turn_show_renders_narration_warning() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            "http://127.0.0.1:8421/v1/sessions/main/captures/warn-1",
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "turn_id": "warn-1",
                    "alias": "fitt-default",
                    "client": "telegram",
                    "model_used": "granite3.3:8b",
                    "backend": "ollama",
                    "fallback_used": False,
                    "started_at": 1779479823.42,
                    "finished_at": 1779479825.81,
                    "dispatched_messages": [],
                    "response": {"choices": []},
                    "tool_calls": [],
                    "prompt_tokens": 5400,
                    "completion_tokens": 89,
                    "context_window": 32768,
                    "prompt_pct_of_window": 16.5,
                    "finish_reason": "stop",
                    "narration_warning": True,
                    "iterations": 1,
                    "status": "ok",
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "show", "warn-1"])
    assert result.exit_code == 0, result.output
    assert "narration warning" in result.output


def test_turn_show_uses_session_flag() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        # Should hit /v1/sessions/retroai/captures/turn-1, not /main/.
        retroai_route = mock.get(
            "http://127.0.0.1:8421/v1/sessions/retroai/captures/turn-1",
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "turn_id": "turn-1",
                    "alias": "fitt-default",
                    "client": "telegram",
                    "model_used": "qwen2.5-coder:14b",
                    "backend": "ollama",
                    "fallback_used": False,
                    "started_at": 1779479823.42,
                    "finished_at": 1779479825.81,
                    "dispatched_messages": [],
                    "response": {"choices": []},
                    "tool_calls": [],
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "context_window": 32768,
                    "prompt_pct_of_window": 0.3,
                    "finish_reason": "stop",
                    "narration_warning": False,
                    "iterations": 1,
                    "status": "ok",
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["turn", "show", "turn-1", "--session", "retroai"])
    assert result.exit_code == 0, result.output
    assert retroai_route.called

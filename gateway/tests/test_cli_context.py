"""Tests for ``fitt context list`` / ``fitt context refresh`` (Slice 7.1).

The CLI hits the running gateway over HTTP. Tests mock the HTTP
responses with respx and verify the CLI renders cleanly.

Auth: the helpers ``_mcp_bearer_token`` and ``_mcp_gateway_url``
read the active config / secrets to find the right URL and
token. Tests write a fake config + secrets pair so those
helpers resolve without going to ``~/.fitt/``.
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
    """Write a minimal config + secrets pair under tmp_path so
    the CLI's load_config calls in ``_mcp_bearer_token`` find
    them. Returns the config-file path."""
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
    """Pin the gateway URL to a known value so respx route
    matching is deterministic."""
    url = "http://127.0.0.1:8421"
    monkeypatch.setenv("FITT_GATEWAY_URL", url)
    return url


# --------------------------------------------------------------- list


def test_context_list_renders_table() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/aliases").mock(
            return_value=httpx.Response(
                200,
                json={
                    "aliases": [
                        {
                            "id": "fitt-default",
                            "primary": {
                                "model_id": "qwen-big",
                                "model": "qwen2.5:14b",
                                "backend": "ollama",
                                "endpoint": "http://localhost:11434",
                            },
                            "fallback": None,
                            "context_window": {
                                "tokens": 32768,
                                "source": "modelfile",
                                "detail": "ollama num_ctx parameter set",
                                "discovered_at": 1779479823.42,
                            },
                            "last_probe": None,
                            "last_eval": None,
                        },
                    ],
                    "generated_at": 1779479823.42,
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["context", "list"])
    assert result.exit_code == 0, result.output
    assert "fitt-default" in result.output
    assert "qwen2.5:14b" in result.output
    # Number is comma-formatted.
    assert "32,768" in result.output
    assert "modelfile" in result.output


def test_context_list_handles_unknown_window() -> None:
    """Bindings whose window discovery hasn't run / failed render
    cleanly as 'unknown'."""
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/aliases").mock(
            return_value=httpx.Response(
                200,
                json={
                    "aliases": [
                        {
                            "id": "fitt-default",
                            "primary": {
                                "model_id": "qwen-big",
                                "model": "qwen2.5:14b",
                                "backend": "ollama",
                                "endpoint": "http://localhost:11434",
                            },
                            "fallback": None,
                            "context_window": None,
                            "last_probe": None,
                            "last_eval": None,
                        },
                    ],
                    "generated_at": 1779479823.42,
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["context", "list"])
    assert result.exit_code == 0, result.output
    assert "unknown" in result.output.lower()


def test_context_list_handles_default_fallback() -> None:
    """The Ollama 2048 default highlights as a warning so
    operators see when they've forgotten to set num_ctx."""
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/aliases").mock(
            return_value=httpx.Response(
                200,
                json={
                    "aliases": [
                        {
                            "id": "fitt-default",
                            "primary": {
                                "model_id": "qwen-big",
                                "model": "qwen2.5:14b",
                                "backend": "ollama",
                                "endpoint": "http://localhost:11434",
                            },
                            "fallback": None,
                            "context_window": {
                                "tokens": 2048,
                                "source": "default",
                                "detail": "ollama documented default",
                                "discovered_at": 1779479823.42,
                            },
                            "last_probe": None,
                            "last_eval": None,
                        },
                    ],
                    "generated_at": 1779479823.42,
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["context", "list"])
    assert result.exit_code == 0, result.output
    assert "2,048" in result.output
    assert "default" in result.output


def test_context_list_handles_transport_failure() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://127.0.0.1:8421/v1/aliases").mock(
            side_effect=httpx.ConnectError("boom"),
        )
        result = runner.invoke(fitt_cli, ["context", "list"])
    assert result.exit_code == 1
    assert "Could not reach gateway" in result.output


# --------------------------------------------------------------- refresh


def test_context_refresh_all_calls_post() -> None:
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        post_route = mock.post("http://127.0.0.1:8421/v1/internal/context-refresh").mock(
            return_value=httpx.Response(
                200,
                json={
                    "refreshed": [
                        {
                            "model_id": "qwen-big",
                            "backend": "ollama",
                            "tokens": 32768,
                            "source": "modelfile",
                            "detail": "ollama num_ctx parameter set",
                            "discovered_at": 1779479823.42,
                        },
                    ]
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["context", "refresh"])
    assert result.exit_code == 0, result.output
    assert post_route.called
    # No alias passed → empty body.
    request = post_route.calls.last.request
    assert request.read() == b"{}"
    assert "qwen-big" in result.output
    assert "32,768" in result.output


def test_context_refresh_single_alias_resolves_to_model_id() -> None:
    """``--alias fitt-default`` must resolve to the alias's
    primary model_id before sending. The gateway's cache keys
    by model_id, not alias."""
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        post_route = mock.post("http://127.0.0.1:8421/v1/internal/context-refresh").mock(
            return_value=httpx.Response(
                200,
                json={
                    "refreshed": [
                        {
                            "model_id": "qwen-big",
                            "tokens": 16384,
                            "source": "modelfile",
                            "detail": "...",
                            "discovered_at": 1779479823.42,
                        }
                    ]
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["context", "refresh", "--alias", "fitt-default"])
    assert result.exit_code == 0, result.output
    request = post_route.calls.last.request
    body = request.read()
    assert b'"qwen-big"' in body  # model_id, not alias name


def test_context_refresh_unknown_alias_exits_nonzero() -> None:
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["context", "refresh", "--alias", "nope"])
    assert result.exit_code == 1
    assert "Unknown alias" in result.output


def test_context_refresh_handles_endpoint_error_envelope() -> None:
    """When the endpoint returns ``{"error": {...}}`` (e.g.
    unknown_model), the CLI prints it and exits nonzero rather
    than a stack trace."""
    runner = CliRunner()
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://127.0.0.1:8421/v1/internal/context-refresh").mock(
            return_value=httpx.Response(
                200,
                json={
                    "error": {
                        "type": "unknown_model",
                        "message": "no model with id 'nope'",
                    }
                },
            ),
        )
        result = runner.invoke(fitt_cli, ["context", "refresh", "--alias", "fitt-default"])
    assert result.exit_code == 1
    assert "unknown_model" in result.output

"""Shared test helpers for building test configs and apps.

Kept out of ``conftest.py`` so that specific tests can explicitly
import only the helpers they need, and to avoid autouse collisions.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from textwrap import dedent

from gateway.config import (
    AllowedToken,
    Config,
    LoggingConfig,
    ModelConfig,
    Secrets,
    ServerConfig,
)

PERSONAL_TOKEN = "TEST_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WRONG_TOKEN = "NOT_THE_RIGHT_TOKEN_XXXXXXXXXXXXXXXXXXXXXXXX"


def build_test_config(tmp_path: Path) -> Config:
    """Build a complete in-memory Config for tests (no YAML files)."""
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080),
        aliases={
            "fitt-default": "qwen-big",
            "fitt-smart": "openrouter-sonnet",
            "fitt-fast": "qwen-small",
        },
        models=[
            ModelConfig(
                id="openrouter-sonnet",
                backend="openrouter",
                model="anthropic/claude-sonnet-4.5",
                cost_per_mtok_in=Decimal("3"),
                cost_per_mtok_out=Decimal("15"),
            ),
            ModelConfig(
                id="qwen-big",
                backend="ollama",
                endpoint="http://laptop.tailnet:11434",
                model="qwen2.5-coder:14b",
                fallback="qwen-small",
            ),
            ModelConfig(
                id="qwen-small",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="qwen2.5-coder:7b",
            ),
        ],
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token=PERSONAL_TOKEN)],
        openrouter_api_key="sk-or-test-xxxxx",
    )
    return cfg


def dedent_strip(s: str) -> str:
    return dedent(s).strip()

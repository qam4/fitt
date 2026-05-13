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
    MemoryConfig,
    ModelConfig,
    Secrets,
    ServerConfig,
)

PERSONAL_TOKEN = "TEST_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WRONG_TOKEN = "NOT_THE_RIGHT_TOKEN_XXXXXXXXXXXXXXXXXXXXXXXX"


def build_test_config(tmp_path: Path, *, memory_enabled: bool = False) -> Config:
    """Build a complete in-memory Config for tests (no YAML files).

    By default memory is disabled so Phase 1 tests don't have to deal
    with identity files and history. Phase 2 tests pass
    ``memory_enabled=True`` and get a memory layer rooted under
    ``tmp_path/fitt``.
    """
    fitt_home = tmp_path / "fitt"
    fitt_home.mkdir(exist_ok=True)
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
        memory=MemoryConfig(
            enabled=memory_enabled,
            max_history_chars=24_000,
            identity_dir=fitt_home / "identity",
            sessions_dir=fitt_home / "sessions",
        ),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token=PERSONAL_TOKEN)],
        openrouter_api_key="sk-or-test-xxxxx",
    )
    return cfg


def dedent_strip(s: str) -> str:
    return dedent(s).strip()


def build_openai_backend_config(tmp_path: Path) -> Config:
    """Build a Config with a single model using the generic `openai` backend.

    Used by router tests that need to exercise the OpenAI-compatible
    dispatch path (Nvidia Build, Groq, LM Studio, vLLM, ...). The
    api key is keyed by the model's id under ``Secrets.api_keys``.
    """
    fitt_home = tmp_path / "fitt"
    fitt_home.mkdir(exist_ok=True)
    cfg = Config(
        server=ServerConfig(host="127.0.0.1", port=8080),
        aliases={"fitt-huge": "nvidia-minimax"},
        models=[
            ModelConfig(
                id="nvidia-minimax",
                backend="openai",
                endpoint="https://integrate.api.nvidia.com/v1",
                model="minimaxai/minimax-m2",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
        ],
        logging=LoggingConfig(dir=tmp_path / "logs", retention_days=7),
        memory=MemoryConfig(
            enabled=False,
            identity_dir=fitt_home / "identity",
            sessions_dir=fitt_home / "sessions",
        ),
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token=PERSONAL_TOKEN)],
        api_keys={"nvidia-minimax": "nvapi-test-xxxxx"},
    )
    return cfg

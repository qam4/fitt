"""Boot-time configuration checks (Principle 11).

``check_missing_api_keys`` walks the loaded config + secrets and
returns a list of warnings for openai-backend models whose
``api_keys`` entry is missing. Called at gateway startup so the
misconfiguration is visible in the logs immediately, rather than
surfacing mid-chat as a misleading "OPENAI_API_KEY not set" error
from LiteLLM. See docs/observed-issues.md for the incident that
motivated this.
"""

from __future__ import annotations

from decimal import Decimal

from gateway.config import (
    AllowedToken,
    Config,
    LoggingConfig,
    MemoryConfig,
    ModelConfig,
    Secrets,
    ServerConfig,
    check_missing_api_keys,
)


def _mk_config(
    *,
    models: list[ModelConfig],
    aliases: dict[str, str],
    api_keys: dict[str, str] | None = None,
) -> Config:
    """Build a minimal valid Config with attached secrets for
    the check_missing_api_keys tests."""
    cfg = Config(
        server=ServerConfig(),
        logging=LoggingConfig(),
        memory=MemoryConfig(enabled=False),
        models=models,
        aliases=aliases,
    )
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="t", token="token")],
        api_keys=api_keys or {},
    )
    return cfg


def _openai_model(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        backend="openai",
        endpoint="https://integrate.api.nvidia.com/v1",
        model=f"provider/{model_id}",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )


def _ollama_model(model_id: str) -> ModelConfig:
    return ModelConfig(
        id=model_id,
        backend="ollama",
        endpoint="http://laptop:11434",
        model="qwen2.5-coder:14b",
        cost_per_mtok_in=Decimal("0"),
        cost_per_mtok_out=Decimal("0"),
    )


def test_returns_empty_when_all_keys_present() -> None:
    """Happy path: every openai-backend model has a matching
    api_keys entry. No warnings."""
    cfg = _mk_config(
        models=[_openai_model("nvidia-a"), _openai_model("nvidia-b")],
        aliases={"fitt-smart": "nvidia-a", "fitt-default": "nvidia-b"},
        api_keys={"nvidia-a": "nvapi-aaa", "nvidia-b": "nvapi-bbb"},
    )
    assert check_missing_api_keys(cfg) == []


def test_returns_warning_for_missing_key() -> None:
    """An openai-backend model with no api_keys entry →
    warning mentioning the model id and the exact YAML entry
    to add."""
    cfg = _mk_config(
        models=[_openai_model("nvidia-a")],
        aliases={"fitt-smart": "nvidia-a"},
        api_keys={},
    )
    warnings = check_missing_api_keys(cfg)
    assert len(warnings) == 1
    assert "nvidia-a" in warnings[0]
    # The warning should include a YAML snippet the operator
    # can paste verbatim.
    assert "api_keys:" in warnings[0]
    assert "nvidia-a:" in warnings[0]
    # And explain the misleading error it's preventing, so
    # the operator connects cause and effect.
    assert "OPENAI_API_KEY" in warnings[0]


def test_ollama_models_skipped() -> None:
    """Non-openai backends don't need api_keys entries. An
    ollama-backend model with no key is fine."""
    cfg = _mk_config(
        models=[_ollama_model("ollama-local")],
        aliases={"fitt-fast": "ollama-local"},
        api_keys={},
    )
    assert check_missing_api_keys(cfg) == []


def test_mixed_backends_reports_only_openai_gaps() -> None:
    """Two models, one openai with missing key, one ollama
    with no key. Only the openai model triggers a warning."""
    cfg = _mk_config(
        models=[
            _openai_model("nvidia-a"),
            _ollama_model("ollama-local"),
        ],
        aliases={
            "fitt-smart": "nvidia-a",
            "fitt-fast": "ollama-local",
        },
        api_keys={},
    )
    warnings = check_missing_api_keys(cfg)
    assert len(warnings) == 1
    assert "nvidia-a" in warnings[0]
    assert "ollama-local" not in warnings[0]


def test_multiple_missing_keys_all_reported() -> None:
    """Each missing key gets its own warning. An operator who
    just added three new models and forgot all three secrets
    sees three clear messages, not one message with a comma
    list."""
    cfg = _mk_config(
        models=[
            _openai_model("nvidia-a"),
            _openai_model("nvidia-b"),
            _openai_model("nvidia-c"),
        ],
        aliases={
            "fitt-smart": "nvidia-a",
            "fitt-default": "nvidia-b",
            "fitt-fast": "nvidia-c",
        },
        api_keys={"nvidia-a": "nvapi-aaa"},
    )
    warnings = check_missing_api_keys(cfg)
    assert len(warnings) == 2
    ids_flagged = {"nvidia-b" in w or "nvidia-c" in w for w in warnings}
    assert ids_flagged == {True}


def test_no_secrets_loaded_skips_check() -> None:
    """CLI commands that load config without secrets
    (``load_secrets_too=False``) must not trigger false
    positives. We skip the check when secrets is None — we
    can't tell whether keys are configured without access to
    the file."""
    cfg = Config(
        server=ServerConfig(),
        logging=LoggingConfig(),
        memory=MemoryConfig(enabled=False),
        models=[_openai_model("nvidia-a")],
        aliases={"fitt-smart": "nvidia-a"},
    )
    assert cfg.secrets is None
    assert check_missing_api_keys(cfg) == []


def test_key_name_mismatch_is_reported() -> None:
    """Model id is ``nvidia-smart`` but api_keys has
    ``fitt-smart`` (the alias name, not the model id — the
    exact mistake that caused the incident).
    Check correctly flags the model.id key as missing."""
    cfg = _mk_config(
        models=[_openai_model("nvidia-smart")],
        aliases={"fitt-smart": "nvidia-smart"},
        api_keys={"fitt-smart": "nvapi-wrong-key"},  # keyed on alias
    )
    warnings = check_missing_api_keys(cfg)
    assert len(warnings) == 1
    assert "nvidia-smart" in warnings[0]

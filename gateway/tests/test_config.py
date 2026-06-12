"""Tests for config and secrets loading.

Covers Phase 1 acceptance criteria 2.4, 2.6, 3.6 and the spec's
unit-test list for config + secrets.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from gateway.config import (
    AliasOrchestrationConfig,
    Config,
    ModelConfig,
    Secrets,
    default_config_path,
    default_secrets_path,
    load_config,
    load_secrets,
)
from gateway.errors import ConfigError, SecretsPermissionError

# --------------------------------------------------------------- helpers


def _valid_config_yaml() -> str:
    return dedent(
        """
        server:
          host: 0.0.0.0
          port: 8080
          log_level: info

        aliases:
          fitt-default: qwen-coder-big
          fitt-smart:   openrouter-sonnet
          fitt-fast:    qwen-coder-small

        models:
          - id: openrouter-sonnet
            backend: openrouter
            model: anthropic/claude-sonnet-4.5
            cost_per_mtok_in:  3.00
            cost_per_mtok_out: 15.00

          - id: qwen-coder-big
            backend: ollama
            endpoint: http://localhost:11434
            model: qwen2.5-coder:14b
            fallback: qwen-coder-small

          - id: qwen-coder-small
            backend: ollama
            endpoint: http://localhost:11434
            model: qwen2.5-coder:7b

        logging:
          dir: /tmp/fitt-logs
          retention_days: 30
        """
    ).strip()


def _valid_secrets_yaml() -> str:
    return dedent(
        """
        allowed_tokens:
          - name: personal
            token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

        openrouter_api_key: sk-or-test-123
        """
    ).strip()


def _write(path: Path, content: str, *, secure: bool = False) -> Path:
    path.write_text(content, encoding="utf-8")
    if secure and os.name != "nt":
        path.chmod(0o600)
    return path


# --------------------------------------------------------------- config tests


def test_config_loads_valid_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)

    cfg = load_config(cp, sp)
    assert isinstance(cfg, Config)
    assert cfg.server.port == 8080
    assert set(cfg.alias_names()) == {"fitt-default", "fitt-smart", "fitt-fast"}
    assert cfg.secrets is not None
    assert cfg.secrets.openrouter_api_key == "sk-or-test-123"


def test_config_rejects_missing_alias_target(tmp_path: Path) -> None:
    # fitt-smart points at a non-existent model id
    bad = _valid_config_yaml().replace("openrouter-sonnet", "does-not-exist", 1)
    cp = _write(tmp_path / "config.yaml", bad)
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    assert "does-not-exist" in str(exc.value)


def test_config_rejects_missing_fallback_target(tmp_path: Path) -> None:
    bad = _valid_config_yaml().replace("fallback: qwen-coder-small", "fallback: not-a-real-id")
    cp = _write(tmp_path / "config.yaml", bad)
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    assert "not-a-real-id" in str(exc.value)


def test_config_rejects_self_fallback(tmp_path: Path) -> None:
    bad = _valid_config_yaml().replace("fallback: qwen-coder-small", "fallback: qwen-coder-big")
    cp = _write(tmp_path / "config.yaml", bad)
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    assert "fallback" in str(exc.value)


def test_config_rejects_ollama_without_endpoint(tmp_path: Path) -> None:
    # Strip the `endpoint: ...` line from the first ollama model.
    lines = _valid_config_yaml().splitlines()
    filtered = []
    dropped = False
    for line in lines:
        if not dropped and "endpoint: http://localhost:11434" in line:
            dropped = True
            continue
        filtered.append(line)
    assert dropped, "test fixture sanity: endpoint line should have been present"
    bad = "\n".join(filtered)

    cp = _write(tmp_path / "config.yaml", bad)
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    assert "endpoint" in str(exc.value)


def test_config_rejects_openai_backend_without_endpoint(tmp_path: Path) -> None:
    """Generic `openai` backend needs an explicit endpoint URL."""
    bad = dedent(
        """
        aliases:
          fitt-huge: nvidia-minimax

        models:
          - id: nvidia-minimax
            backend: openai
            model: minimaxai/minimax-m2
        """
    ).strip()
    cp = _write(tmp_path / "config.yaml", bad)
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    msg = str(exc.value)
    assert "endpoint" in msg
    assert "openai" in msg


def test_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "does-not-exist.yaml", load_secrets_too=False)
    assert "not found" in str(exc.value)


def test_alias_resolution_with_fallback(tmp_path: Path) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    cfg = load_config(cp, sp)

    chain = cfg.resolve_alias("fitt-default")
    assert [m.id for m in chain] == ["qwen-coder-big", "qwen-coder-small"]


def test_alias_resolution_without_fallback(tmp_path: Path) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    cfg = load_config(cp, sp)

    chain = cfg.resolve_alias("fitt-smart")
    assert [m.id for m in chain] == ["openrouter-sonnet"]


def test_alias_resolve_unknown_raises(tmp_path: Path) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    cfg = load_config(cp, sp)

    with pytest.raises(KeyError):
        cfg.resolve_alias("fitt-bogus")


# --------------------------------------------------------------- secrets tests


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check only")
def test_secrets_rejects_world_readable(tmp_path: Path) -> None:
    sp = tmp_path / "secrets.yaml"
    sp.write_text(_valid_secrets_yaml(), encoding="utf-8")
    sp.chmod(0o644)  # readable by others

    with pytest.raises(SecretsPermissionError):
        load_secrets(sp)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission check only")
def test_secrets_accepts_0600(tmp_path: Path) -> None:
    sp = tmp_path / "secrets.yaml"
    sp.write_text(_valid_secrets_yaml(), encoding="utf-8")
    sp.chmod(0o600)

    secrets = load_secrets(sp)
    assert isinstance(secrets, Secrets)


def test_secrets_api_key_lookup(tmp_path: Path) -> None:
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    secrets = load_secrets(sp)
    assert secrets.api_key_for("openrouter") == "sk-or-test-123"
    assert secrets.api_key_for("anthropic") is None
    assert secrets.api_key_for("ollama") is None


def test_secrets_api_key_for_openai_backend(tmp_path: Path) -> None:
    """Generic OpenAI-compatible backend keys are looked up by model id."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: personal
            token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

        api_keys:
          nvidia-minimax: nvapi-xyz
          groq-llama:     gsk-abc
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    secrets = load_secrets(sp)

    assert secrets.api_key_for("openai", model_id="nvidia-minimax") == "nvapi-xyz"
    assert secrets.api_key_for("openai", model_id="groq-llama") == "gsk-abc"
    # Unknown model id: None, not a KeyError.
    assert secrets.api_key_for("openai", model_id="unknown-id") is None
    # No model id passed: None (caller didn't identify the model).
    assert secrets.api_key_for("openai") is None


def test_secrets_api_keys_as_list_gets_friendly_error(tmp_path: Path) -> None:
    """The most common foot-gun: writing ``api_keys`` as a YAML
    list of single-key dicts (``- key: val``) instead of a flat
    mapping. Pydantic rejects it but with a noisy nested error
    message; we wrap it with a one-line "fix it like this"
    explanation that gives the operator the right shape verbatim.

    Pinned 2026-05-13 after a live first-boot debug session
    where the operator hit this and had to spelunk through
    `docker logs` + Pydantic dump to figure it out."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: personal
            token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

        api_keys:
          - nvidia-qwen: nvapi-xyz
          - groq-llama: gsk-abc
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    with pytest.raises(ConfigError) as exc:
        load_secrets(sp)
    msg = str(exc.value)
    assert "api_keys" in msg
    assert "must be a YAML mapping" in msg
    # Helpful examples in the message.
    assert "your-model-id: nvapi" in msg
    assert "leading `-`" in msg


# --------------------------------------------------------------- client tags


def test_allowed_token_accepts_client_tag(tmp_path: Path) -> None:
    """`client:` tags on allowed_tokens drive per-client approval policy."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: ide
            token: TOKEN_IDE_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
            client: ide
          - name: tg
            token: TOKEN_TG_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
            client: telegram
          - name: untagged
            token: TOKEN_UT_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    secrets = load_secrets(sp)

    assert len(secrets.allowed_tokens) == 3
    assert secrets.allowed_tokens[0].client == "ide"
    assert secrets.allowed_tokens[1].client == "telegram"
    assert secrets.allowed_tokens[2].client is None


def test_allowed_token_rejects_unknown_client(tmp_path: Path) -> None:
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: bogus
            token: TOKEN_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
            client: admin
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    with pytest.raises(ConfigError):
        load_secrets(sp)


def test_client_for_returns_configured_tag(tmp_path: Path) -> None:
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: ide
            token: IDE_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            client: ide
          - name: tg
            token: TG_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
            client: telegram
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    secrets = load_secrets(sp)

    assert secrets.client_for("IDE_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA") == "ide"
    assert secrets.client_for("TG_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB") == "telegram"


def test_client_for_untagged_token_defaults_to_webui(tmp_path: Path) -> None:
    """Missing client tag = treated as webui (least-trusted)."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: legacy
            token: LEGACY_TOKEN_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    secrets = load_secrets(sp)
    assert secrets.client_for("LEGACY_TOKEN_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC") == "webui"


def test_client_for_unknown_token_returns_unknown(tmp_path: Path) -> None:
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    secrets = load_secrets(sp)
    assert secrets.client_for("not-a-real-token") == "unknown"


# ------------------------------------------------------------ default path tests


def test_default_paths_respect_fitt_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "config.yaml"
    assert default_secrets_path() == tmp_path / "secrets.yaml"


def test_default_paths_respect_explicit_envs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    custom_cfg = tmp_path / "other.yaml"
    custom_secrets = tmp_path / "s.yaml"
    monkeypatch.setenv("FITT_CONFIG_PATH", str(custom_cfg))
    monkeypatch.setenv("FITT_SECRETS_PATH", str(custom_secrets))
    assert default_config_path() == custom_cfg
    assert default_secrets_path() == custom_secrets


# ---------------------------------------------- FITT_PORT env override


def test_fitt_port_env_overrides_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FITT_PORT wins over the port written in config.yaml."""
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    monkeypatch.setenv("FITT_PORT", "8421")

    cfg = load_config(cp, sp)
    assert cfg.server.port == 8421


def test_fitt_port_unset_keeps_yaml_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    monkeypatch.delenv("FITT_PORT", raising=False)

    cfg = load_config(cp, sp)
    # _valid_config_yaml() writes port: 8080 in its sample.
    assert cfg.server.port == 8080


def test_fitt_port_rejects_nonint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    monkeypatch.setenv("FITT_PORT", "not-a-port")

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    assert "FITT_PORT" in str(exc.value)


def test_fitt_port_rejects_out_of_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _write(tmp_path / "config.yaml", _valid_config_yaml())
    sp = _write(tmp_path / "secrets.yaml", _valid_secrets_yaml(), secure=True)
    monkeypatch.setenv("FITT_PORT", "999999")

    with pytest.raises(ConfigError) as exc:
        load_config(cp, sp)
    assert "out of range" in str(exc.value)


# --------------------------------------------------------------- allowed_tokens validators


def test_secrets_rejects_duplicate_token_value(tmp_path: Path) -> None:
    """Two entries with the same token value: only the first
    matches at runtime, the rest are dead weight. Almost always
    a copy-paste mistake. Pin the validator's rejection so a
    future schema tweak doesn't silently restore the
    order-dependent behaviour."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: phone
            token: SHARED_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAA
            client: telegram
          - name: ide
            token: SHARED_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAA
            client: ide
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    with pytest.raises(ConfigError) as exc:
        load_secrets(sp)
    msg = str(exc.value)
    assert "duplicate token" in msg
    assert "phone" in msg
    assert "ide" in msg


def test_secrets_rejects_duplicate_name(tmp_path: Path) -> None:
    """Two entries with the same name make audit logs and
    deprecation warnings ambiguous about which entry produced
    them."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: bot
            token: TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            client: telegram
          - name: bot
            token: TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
            client: ide
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    with pytest.raises(ConfigError) as exc:
        load_secrets(sp)
    assert "duplicate name" in str(exc.value)


def test_secrets_rejects_duplicate_client_tag(tmp_path: Path) -> None:
    """Two entries claiming the same client tag would make the
    ``[t for t in tokens if t.client == 'telegram']`` lookup
    used by the bot pick whichever ends up first in the list.
    Refuse to start so the operator picks one explicitly."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: phone-old
            token: TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
            client: telegram
          - name: phone-new
            token: TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
            client: telegram
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    with pytest.raises(ConfigError) as exc:
        load_secrets(sp)
    msg = str(exc.value)
    assert "telegram" in msg
    assert "phone-old" in msg
    assert "phone-new" in msg


def test_secrets_allows_multiple_untagged_tokens(tmp_path: Path) -> None:
    """Untagged tokens (ad-hoc curl, testing) should be allowed
    in any quantity — they don't drive the by-tag lookups, so
    there's no ambiguity to refuse over. Pinned so a stricter
    validator doesn't accidentally regress this."""
    secrets_yaml = dedent(
        """
        allowed_tokens:
          - name: scratch-1
            token: TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
          - name: scratch-2
            token: TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
        """
    ).strip()
    sp = _write(tmp_path / "secrets.yaml", secrets_yaml, secure=True)
    secrets = load_secrets(sp)
    assert len(secrets.allowed_tokens) == 2


# --------------------------------------------------------------- orchestration gate (Phase 12 task 10)


def _orchestration_config(
    orchestration: dict[str, AliasOrchestrationConfig] | None = None,
) -> Config:
    """Minimal valid Config for exercising the per-alias orchestrate gate."""
    from decimal import Decimal

    return Config(
        aliases={"fitt-default": "m1"},
        models=[
            ModelConfig(
                id="m1",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="qwen3:8b",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            )
        ],
        orchestration=orchestration or {},
    )


def test_orchestration_defaults_off() -> None:
    cfg = _orchestration_config()
    assert cfg.is_orchestrated("fitt-default") is False
    # Unknown alias is not an error for the gate query — just False.
    assert cfg.is_orchestrated("does-not-exist") is False


def test_orchestration_enabled_per_alias() -> None:
    cfg = _orchestration_config({"fitt-default": AliasOrchestrationConfig(enabled=True)})
    assert cfg.is_orchestrated("fitt-default") is True


def test_orchestration_entry_for_unknown_alias_rejected() -> None:
    with pytest.raises(ValueError, match="no such alias"):
        _orchestration_config({"ghost": AliasOrchestrationConfig(enabled=True)})


# --------------------------------------------------------------- orchestration budgets + planner_alias (Phase 12 task 11)


def _two_alias_config(
    orchestration: dict[str, AliasOrchestrationConfig] | None = None,
) -> Config:
    """Valid Config with two aliases, for planner_alias cross-references."""
    from decimal import Decimal

    return Config(
        aliases={"fitt-default": "m1", "fitt-smart": "m2"},
        models=[
            ModelConfig(
                id="m1",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="qwen3:8b",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
            ModelConfig(
                id="m2",
                backend="openrouter",
                model="anthropic/claude-sonnet-4.5",
                cost_per_mtok_in=Decimal("0"),
                cost_per_mtok_out=Decimal("0"),
            ),
        ],
        orchestration=orchestration or {},
    )


def test_orchestration_budget_fields_parse() -> None:
    cfg = _two_alias_config(
        {
            "fitt-default": AliasOrchestrationConfig(
                enabled=True,
                planner_iterations=1,
                executor_iterations=20,
            )
        }
    )
    ocfg = cfg.orchestration["fitt-default"]
    assert ocfg.planner_iterations == 1
    assert ocfg.executor_iterations == 20
    # Default when omitted is None (orchestrator falls back to its defaults).
    assert ocfg.planner_alias == ""


def test_orchestration_planner_alias_must_be_configured() -> None:
    # A planner_alias pointing at a known alias is accepted.
    cfg = _two_alias_config(
        {"fitt-default": AliasOrchestrationConfig(enabled=True, planner_alias="fitt-smart")}
    )
    assert cfg.orchestration["fitt-default"].planner_alias == "fitt-smart"


def test_orchestration_planner_alias_unknown_rejected() -> None:
    with pytest.raises(ValueError, match="planner_alias"):
        _two_alias_config(
            {"fitt-default": AliasOrchestrationConfig(enabled=True, planner_alias="ghost")}
        )


def test_orchestration_iterations_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AliasOrchestrationConfig(planner_iterations=0)
    with pytest.raises(ValueError):
        AliasOrchestrationConfig(executor_iterations=0)

"""Configuration loading and validation.

Two files:
  * ~/.fitt/config.yaml — non-secret, user-specific (aliases, models,
    endpoints, cost rates, logging).
  * ~/.fitt/secrets.yaml — secrets (Bearer tokens, API keys). Mode-
    checked on load to refuse world-readable files.

Both override-able via env vars for testing:
  * FITT_CONFIG_PATH
  * FITT_SECRETS_PATH
  * FITT_HOME (overrides ~/.fitt as the default parent dir)
"""

from __future__ import annotations

import os
import stat
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError, SecretsPermissionError

# --------------------------------------------------------------------- paths


def fitt_home() -> Path:
    """Return the directory where ~/.fitt/ lives.

    Env var ``FITT_HOME`` takes precedence (used by tests). Otherwise
    ``~/.fitt/`` expanded via ``Path.home()``.
    """
    env = os.environ.get("FITT_HOME")
    if env:
        return Path(env)
    return Path.home() / ".fitt"


def default_config_path() -> Path:
    env = os.environ.get("FITT_CONFIG_PATH")
    return Path(env) if env else fitt_home() / "config.yaml"


def default_secrets_path() -> Path:
    env = os.environ.get("FITT_SECRETS_PATH")
    return Path(env) if env else fitt_home() / "secrets.yaml"


# ---------------------------------------------------------------- model schema

Backend = Literal["openrouter", "anthropic", "ollama", "openai"]


class ModelConfig(BaseModel):
    """One concrete model entry in config.yaml."""

    model_config = ConfigDict(extra="forbid")

    id: str
    backend: Backend
    model: str  # The upstream model identifier (LiteLLM's model string)
    endpoint: str | None = None  # Required for ollama and openai
    cost_per_mtok_in: Decimal = Decimal("0")
    cost_per_mtok_out: Decimal = Decimal("0")
    fallback: str | None = None  # Another model id in this config

    @model_validator(mode="after")
    def _endpoint_required_for_local_backends(self) -> ModelConfig:
        # Ollama and the generic 'openai' backend both require an
        # endpoint URL. 'openai' is used for any OpenAI-compatible
        # provider we don't have a dedicated backend for (Nvidia
        # Build, Groq, Together, LM Studio, vLLM, ...).
        if self.backend in ("ollama", "openai") and not self.endpoint:
            raise ValueError(f"model {self.id!r}: backend {self.backend!r} requires 'endpoint'")
        return self


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8421
    log_level: str = "info"
    log_bodies: bool = False


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dir: Path = Field(default_factory=lambda: fitt_home() / "logs")
    retention_days: int = 30

    @field_validator("dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class MemoryConfig(BaseModel):
    """Phase 2 memory settings.

    All fields are optional; defaults produce a working memory layer
    under FITT_HOME. Set enabled=false to revert to Phase 1 behaviour
    (stateless chat, no identity injection, no persistence).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_history_chars: int = 24_000
    identity_dir: Path = Field(default_factory=lambda: fitt_home() / "identity")
    sessions_dir: Path = Field(default_factory=lambda: fitt_home() / "sessions")
    # Phase 5 — lessons ceiling.
    max_lessons: int = 50
    # Phase 5 — history retention for the background pruner. Default
    # 90 days. Files past this get removed by the history pruner;
    # turns inside files past this are dropped from injection via
    # the decay layer.
    history_max_days: int = 90

    # Tool-output artifact hoisting (Claude Code layer 0; see
    # docs/hallucinations-and-poisoning.md proposed item 4).
    # Any tool payload over ``tool_output_max_inline_bytes`` gets
    # written to ``sessions/<key>/artifacts/<day>/<uuid>.txt`` and
    # the in-context content becomes a preview + path pointer.
    # ``tool_output_preview_bytes`` is the head we keep inline; it
    # gets clamped to at most half the threshold so the preview
    # never approaches the budget it was meant to duck under.
    tool_output_max_inline_bytes: int = 8192
    tool_output_preview_bytes: int = 2048

    @field_validator("identity_dir", "sessions_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


class Config(BaseModel):
    """Top-level configuration loaded from config.yaml."""

    model_config = ConfigDict(extra="forbid")

    server: ServerConfig = Field(default_factory=ServerConfig)
    aliases: dict[str, str]
    models: list[ModelConfig]
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # ``tools:`` block is Phase 4+: per-tool approval buckets,
    # wildcards for MCP, per-client overrides. Shape is
    # intentionally loose at the Config layer (any mapping) and
    # parsed strictly downstream by ToolPolicy. Absent block =
    # all defaults from the registry's client table.
    tools: dict[str, Any] | None = None

    # ``events:`` block is Phase 4.5 Task 10 event-log pruning.
    # Shape:
    #   events:
    #     max_age_days: 90
    # Loose dict to match the ``tools:`` pattern — parsed
    # downstream by the EventPruner wiring.
    events: dict[str, Any] | None = None

    # ``mcp_servers:`` block is Phase 4+: zero or more MCP
    # subprocess servers the gateway spawns and proxies. Parsed
    # strictly by MCPServerConfig at startup in app.py; we keep
    # the config-layer type loose so a missing or empty block
    # doesn't break config loading.
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)

    # Populated after secrets load. Not serialised.
    secrets: Secrets | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _validate_graph(self) -> Config:
        ids = {m.id for m in self.models}
        # Every alias target must exist
        for alias, target in self.aliases.items():
            if target not in ids:
                raise ValueError(
                    f"alias {alias!r} → {target!r} but no model with that id is configured"
                )
        # Every fallback target must exist
        for m in self.models:
            if m.fallback is not None and m.fallback not in ids:
                raise ValueError(
                    f"model {m.id!r} fallback={m.fallback!r} but no model with that id is configured"
                )
            if m.fallback == m.id:
                raise ValueError(f"model {m.id!r} cannot have itself as fallback")
        return self

    # ------------------------------------------------------------- lookup helpers

    def resolve_alias(self, alias: str) -> list[ModelConfig]:
        """Resolve an alias to its primary model plus fallback chain.

        Returns a list of length 1 or 2 (primary, optional fallback).
        Raises KeyError if the alias isn't configured (callers map this
        to the domain-specific UnknownAlias exception).
        """
        if alias not in self.aliases:
            raise KeyError(alias)
        primary_id = self.aliases[alias]
        primary = self._by_id(primary_id)
        if primary.fallback:
            return [primary, self._by_id(primary.fallback)]
        return [primary]

    def _by_id(self, model_id: str) -> ModelConfig:
        for m in self.models:
            if m.id == model_id:
                return m
        # Should be unreachable after _validate_graph.
        raise KeyError(f"no model with id {model_id!r}")

    def alias_names(self) -> list[str]:
        return sorted(self.aliases.keys())


# -------------------------------------------------------------- secrets schema


class AllowedToken(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    token: str
    # Optional client tag — drives per-client approval defaults in
    # Phase 4+. When absent, the client is treated as "webui" (least
    # trusted) so older secrets files stay safe by default.
    client: Literal["ide", "telegram", "webui", "cli"] | None = None


class TelegramSecrets(BaseModel):
    """Reserved for Phase 3; unused in Phase 1."""

    model_config = ConfigDict(extra="forbid")

    bot_token: str
    allowlist_user_ids: list[int] = Field(default_factory=list)


class Secrets(BaseModel):
    """Secrets loaded from secrets.yaml. Permission-checked on load."""

    model_config = ConfigDict(extra="forbid")

    allowed_tokens: list[AllowedToken]
    openrouter_api_key: str | None = None
    anthropic_api_key: str | None = None
    # Per-model keys for generic OpenAI-compatible backends (Nvidia
    # Build, Groq, Together, Fireworks, ...). Keyed by the model's
    # ``id`` in config.yaml. Populate only for backends that need
    # authentication; endpoints like a local vLLM or LM Studio
    # usually don't.
    api_keys: dict[str, str] = Field(default_factory=dict)
    telegram: TelegramSecrets | None = None

    def api_key_for(self, backend: Backend, *, model_id: str | None = None) -> str | None:
        match backend:
            case "openrouter":
                return self.openrouter_api_key
            case "anthropic":
                return self.anthropic_api_key
            case "openai":
                # Generic OpenAI-compatible backends look up their
                # key by the model's id so one key per provider is
                # easy to manage.
                if model_id is None:
                    return None
                return self.api_keys.get(model_id)
            case "ollama":
                return None

    def client_for(self, token: str) -> str:
        """Return the client tag for an incoming Bearer token.

        Matches the first allowed token whose value equals ``token``.
        Falls back to ``"webui"`` (least-trusted client) when the
        matching token has no ``client:`` field configured. Returns
        ``"unknown"`` only if no token matches, which should never
        happen for a request that already passed auth.
        """
        import secrets as _secrets

        for entry in self.allowed_tokens:
            if _secrets.compare_digest(entry.token, token):
                return entry.client or "webui"
        return "unknown"


# ----------------------------------------------------------------- loader


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as e:
        raise ConfigError(f"{path} not found. Copy from configs/*.example.yaml.") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a YAML mapping at top level")
    return data


def _check_secrets_permissions(path: Path) -> None:
    """Refuse to load if secrets.yaml is readable by anyone but the owner.

    POSIX: check mode bits. Windows: best-effort — warn rather than
    fail because NTFS ACL inspection from Python is awkward and most
    single-user machines are fine.
    """
    if os.name == "nt":
        # On Windows we rely on the user having run `icacls` or on the
        # default `%USERPROFILE%\.fitt\` being inside their profile,
        # which is ACL'd to them by default. Documented in
        # docs/accounts-setup.md. No hard check here.
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise SecretsPermissionError(
            f"{path} is readable or writable by group/other (mode={oct(mode)}). "
            f"Run: chmod 0600 {path}"
        )


def load_secrets(path: Path | None = None) -> Secrets:
    p = path or default_secrets_path()
    _check_secrets_permissions(p)
    raw = _read_yaml(p)
    try:
        return Secrets.model_validate(raw)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(f"{p} failed validation: {e}") from e


def load_config(
    config_path: Path | None = None,
    secrets_path: Path | None = None,
    *,
    load_secrets_too: bool = True,
) -> Config:
    """Load config.yaml (and secrets.yaml unless told not to)."""
    cp = config_path or default_config_path()
    raw = _read_yaml(cp)
    try:
        cfg = Config.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"{cp} failed validation: {e}") from e
    # FITT_PORT env var overrides the YAML port. Applied here rather
    # than in ServerConfig's validator so tests that build
    # ServerConfig instances directly aren't subject to whatever
    # FITT_PORT happens to be set in the developer's shell. The
    # Docker deployment always goes through load_config, so the
    # override lands where it matters.
    env_port = os.environ.get("FITT_PORT")
    if env_port:
        try:
            parsed = int(env_port)
        except ValueError as e:
            raise ConfigError(f"FITT_PORT must be an integer, got {env_port!r}") from e
        if not 1 <= parsed <= 65535:
            raise ConfigError(f"FITT_PORT out of range: {parsed}")
        cfg.server.port = parsed
    if load_secrets_too:
        cfg.secrets = load_secrets(secrets_path)
    return cfg


# ----------------------------------------------------------------- boot-time checks


def check_missing_api_keys(config: Config) -> list[str]:
    """Return a list of human-readable warnings for
    ``openai``-backend models whose ``api_keys`` entry is missing
    from ``secrets.yaml``.

    Principle 11 (fail loud on detectable misconfigurations).
    The old behaviour was: gateway starts cleanly, the first
    request to the affected alias dispatches through LiteLLM's
    fallback OpenAI client, which raises
    ``AuthenticationError: the api_key client option must be
    set either by passing api_key to the client or by setting
    OPENAI_API_KEY env variable``. The message is technically
    correct but misleading — the operator's fix isn't to set
    ``OPENAI_API_KEY``, it's to add the matching entry in
    ``secrets.api_keys.<model.id>``.

    This check runs at gateway boot. Missing keys don't block
    startup — other aliases might work fine — but each missing
    key yields a warning string the caller is expected to log
    at ERROR level so the misconfiguration is unmissable.

    Returns an empty list when everything checks out. Safe to
    call with ``config.secrets`` as ``None``; in that case we
    skip the check (we can't tell whether keys are missing
    without secrets, and ``load_secrets_too=False`` is an
    explicit opt-out used by the CLI for non-dispatch commands
    like ``fitt session new``).
    """
    if config.secrets is None:
        return []
    warnings: list[str] = []
    for model in config.models:
        if model.backend != "openai":
            continue
        if model.id not in config.secrets.api_keys:
            warnings.append(
                f"model {model.id!r} has backend=openai but no "
                f"api_keys.{model.id} entry in secrets.yaml. Any "
                f"alias dispatching to this model will fail at "
                f"first request with a misleading "
                f"'OPENAI_API_KEY not set' error. Add to "
                f"secrets.yaml:\n"
                f"    api_keys:\n"
                f"      {model.id}: <your-api-key>"
            )
    return warnings

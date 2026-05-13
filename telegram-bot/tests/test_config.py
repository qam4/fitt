"""Tests for TelegramBotConfig loading.

The regression motivating this file: pre-fix, ``load_bot_config``
ignored the ``FITT_GATEWAY_URL`` env var and always constructed
``http://127.0.0.1:<server.port>`` from the gateway's own
config.yaml. In the docker-compose deployment the bot and
gateway live in separate containers, so 127.0.0.1 points at the
bot itself - no gateway there - and every ``/model`` command
produced a ``gateway.list_aliases.failed`` warning.

The fix makes ``FITT_GATEWAY_URL`` the primary source when set,
falling back to the old localhost construction for the bare-metal
install path where bot and gateway share a host.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from fitt_telegram_bot.config import load_bot_config


def _seed_fitt_home(fitt_home: Path, *, port: int = 8080) -> None:
    """Write a minimal valid config+secrets pair into FITT_HOME.

    Uses the real schema so ``gateway.config.load_config`` actually
    validates; otherwise we'd only be testing the bot's override
    logic in a vacuum.
    """
    (fitt_home / "config.yaml").write_text(
        dedent(
            f"""
            server:
              host: 0.0.0.0
              port: {port}
              log_level: info

            aliases:
              fitt-default: qwen-coder-big

            models:
              - id: qwen-coder-big
                backend: ollama
                endpoint: http://192.168.1.10:11434
                model: qwen2.5-coder:14b
            """
        ).strip(),
        encoding="utf-8",
    )
    secrets = fitt_home / "secrets.yaml"
    secrets.write_text(
        dedent(
            """
            allowed_tokens:
              - name: phone-bot
                token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
                client: telegram

            telegram:
              bot_token: 987654:REAL-ISH-TOKEN-PLACEHOLDER-FOR-TESTS
              allowlist_user_ids:
                - 42
            """
        ).strip(),
        encoding="utf-8",
    )
    if os.name != "nt":
        secrets.chmod(0o600)


def test_gateway_url_uses_env_var_when_set(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FITT_GATEWAY_URL wins over the localhost fallback.

    In docker-compose the bot and gateway are separate containers
    on a bridge network; 127.0.0.1 inside the bot container is the
    bot itself, so it never works.
    """
    _seed_fitt_home(isolate_fitt_home, port=8080)
    monkeypatch.setenv("FITT_GATEWAY_URL", "http://gateway:8080")

    _cfg, bot_cfg = load_bot_config()
    assert bot_cfg.gateway_url == "http://gateway:8080"


def test_gateway_url_falls_back_to_localhost_without_env(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare-metal install: no env var, use config.yaml's port over loopback."""
    _seed_fitt_home(isolate_fitt_home, port=8080)
    monkeypatch.delenv("FITT_GATEWAY_URL", raising=False)

    _cfg, bot_cfg = load_bot_config()
    assert bot_cfg.gateway_url == "http://127.0.0.1:8080"


def test_gateway_url_env_blank_string_is_ignored(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank env var falls back to the localhost default.

    docker-compose makes it easy to set FITT_GATEWAY_URL="" via a
    broken override; treating the empty string as "use me" would
    leave the bot trying to reach the gateway at an empty URL.
    """
    _seed_fitt_home(isolate_fitt_home, port=8080)
    monkeypatch.setenv("FITT_GATEWAY_URL", "   ")

    _cfg, bot_cfg = load_bot_config()
    assert bot_cfg.gateway_url == "http://127.0.0.1:8080"


def test_gateway_url_localhost_uses_configured_port(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port comes from config.yaml's server.port, not a hard-coded 8080."""
    _seed_fitt_home(isolate_fitt_home, port=9999)
    monkeypatch.delenv("FITT_GATEWAY_URL", raising=False)

    _cfg, bot_cfg = load_bot_config()
    assert bot_cfg.gateway_url == "http://127.0.0.1:9999"


# --------------------------------------------------------------- bearer-token lookup


def test_bearer_token_picked_by_telegram_tag_not_by_position(
    isolate_fitt_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot must pick its bearer token by the
    ``client: telegram`` tag, not by list position.

    Pre-fix the bot used ``allowed_tokens[0]``; once an operator
    added a second token (e.g. for opencode), reordering
    secrets.yaml could silently swap which token the bot used.
    Now the bot looks up the entry tagged ``client: telegram``;
    the gateway-side ``Secrets`` validator rejects multiple
    entries with the same client tag, so the lookup is
    deterministic.

    Pin: list the IDE token first; the bot still picks the
    telegram-tagged one."""
    fitt_home = isolate_fitt_home
    (fitt_home / "config.yaml").write_text(
        dedent(
            """
            server:
              host: 0.0.0.0
              port: 8080
              log_level: info

            aliases:
              fitt-default: qwen-coder-big

            models:
              - id: qwen-coder-big
                backend: ollama
                endpoint: http://192.168.1.10:11434
                model: qwen2.5-coder:14b
            """
        ).strip(),
        encoding="utf-8",
    )
    secrets = fitt_home / "secrets.yaml"
    secrets.write_text(
        dedent(
            """
            allowed_tokens:
              # IDE token first to make the position lookup
              # diverge from the tag lookup if it ever
              # regressed.
              - name: ide-token
                token: IDE_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
                client: ide
              - name: phone-bot
                token: TELEGRAM_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
                client: telegram

            telegram:
              bot_token: 987654:REAL-ISH-TOKEN-PLACEHOLDER-FOR-TESTS
              allowlist_user_ids:
                - 42
            """
        ).strip(),
        encoding="utf-8",
    )
    if os.name != "nt":
        secrets.chmod(0o600)
    monkeypatch.delenv("FITT_GATEWAY_URL", raising=False)

    _cfg, bot_cfg = load_bot_config()
    assert bot_cfg.bearer_token == "TELEGRAM_TOKEN_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def test_bot_config_errors_when_no_telegram_tagged_token(
    isolate_fitt_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a ``client: telegram`` tag on at least one
    token, the bot can't tell which credential is its own.
    Refuse to start with a clear message rather than silently
    grabbing some other client's token."""
    from gateway.errors import ConfigError

    fitt_home = isolate_fitt_home
    (fitt_home / "config.yaml").write_text(
        dedent(
            """
            server:
              host: 0.0.0.0
              port: 8080
              log_level: info

            aliases:
              fitt-default: qwen-coder-big

            models:
              - id: qwen-coder-big
                backend: ollama
                endpoint: http://192.168.1.10:11434
                model: qwen2.5-coder:14b
            """
        ).strip(),
        encoding="utf-8",
    )
    secrets = fitt_home / "secrets.yaml"
    secrets.write_text(
        dedent(
            """
            allowed_tokens:
              - name: ide-token
                token: IDE_TOKEN_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
                client: ide

            telegram:
              bot_token: 987654:REAL-ISH-TOKEN-PLACEHOLDER-FOR-TESTS
              allowlist_user_ids:
                - 42
            """
        ).strip(),
        encoding="utf-8",
    )
    if os.name != "nt":
        secrets.chmod(0o600)
    monkeypatch.delenv("FITT_GATEWAY_URL", raising=False)

    with pytest.raises(ConfigError) as exc:
        load_bot_config()
    assert "client: telegram" in str(exc.value)

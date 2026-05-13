"""Configuration bundle for the Telegram bot.

Reads the gateway's ``~/.fitt/config.yaml`` and ``secrets.yaml`` to
avoid duplicating file formats and key names. Pulls out only the
slices the bot actually needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from gateway.config import (
    Config,
    default_config_path,
    default_secrets_path,
    fitt_home,
    load_config,
)
from gateway.errors import ConfigError


@dataclass(frozen=True)
class TelegramBotConfig:
    """Everything the bot needs to run."""

    bot_token: str
    allowlist_user_ids: frozenset[int]
    gateway_url: str
    bearer_token: str
    sessions_dir: Path
    prefs_path: Path
    log_level: str


def load_bot_config(
    config_path: Path | None = None,
    secrets_path: Path | None = None,
) -> tuple[Config, TelegramBotConfig]:
    """Load the gateway config + secrets and return both the full
    ``Config`` (handy for passing the session registry around) and
    the bot-specific slice.

    Raises ``ConfigError`` if any required piece is missing.
    """
    cfg = load_config(
        config_path or default_config_path(),
        secrets_path or default_secrets_path(),
    )
    secrets = cfg.secrets
    if secrets is None or not secrets.telegram:
        raise ConfigError(
            "No telegram block in secrets.yaml. Add your bot token and "
            "allowlist before starting the bot."
        )
    tg = secrets.telegram
    if not tg.bot_token or tg.bot_token.startswith("123456:ABC-"):
        raise ConfigError(
            "Telegram bot_token is missing or still the placeholder. "
            "Get one from @BotFather and put it in secrets.yaml."
        )
    # An empty list is a valid 'lock everyone out' state and NOT an
    # error - we log it loudly at runtime instead.

    # The bot authenticates to the gateway with the token tagged
    # ``client: telegram`` in ``secrets.yaml``. Picking by tag
    # (rather than position) means reordering ``allowed_tokens``
    # doesn't accidentally swap which token the bot uses, and it
    # forces the operator to think about which token is which —
    # the ``Secrets`` validator already rejects multiple tokens
    # claiming the same client tag, so this lookup is
    # deterministic by construction.
    if not secrets.allowed_tokens:
        raise ConfigError(
            "No allowed_tokens in secrets.yaml. The bot needs a Bearer "
            "token tagged `client: telegram` to reach the gateway."
        )
    telegram_tokens = [t for t in secrets.allowed_tokens if t.client == "telegram"]
    if not telegram_tokens:
        raise ConfigError(
            "No allowed_tokens entry tagged `client: telegram`. The bot "
            "needs an explicit `client: telegram` tag on its bearer "
            "token entry in secrets.yaml so picking the right one isn't "
            "order-dependent. Add `client: telegram` to the token entry "
            "the bot should authenticate with."
        )
    # The Secrets validator rejects multiple tokens claiming the
    # same client tag at config-load time, so we'll never see
    # >1 here. Defensive index-0 in case a future schema tweak
    # relaxes that and someone forgets to update this lookup.
    bearer = telegram_tokens[0].token

    # Where the bot reaches the gateway. Precedence:
    #   1. FITT_GATEWAY_URL env var — the explicit knob, set by
    #      docker-compose to `http://gateway:8080` so the bot
    #      resolves the gateway by compose service name on the
    #      bridge network.
    #   2. Fallback: `http://127.0.0.1:<port>` assuming the bot
    #      and gateway share a host (the bare-metal install
    #      pattern we had pre-Phase-3.5).
    # Don't quietly accept an empty string; catch the common
    # mistake of leaving the env var unset to "" in a broken
    # compose override.
    env_url = os.environ.get("FITT_GATEWAY_URL", "").strip()
    if env_url:
        gateway_url = env_url
    else:
        gateway_url = f"http://127.0.0.1:{cfg.server.port}"

    prefs_path = fitt_home() / "telegram" / "prefs.json"

    bot_cfg = TelegramBotConfig(
        bot_token=tg.bot_token,
        allowlist_user_ids=frozenset(tg.allowlist_user_ids),
        gateway_url=gateway_url,
        bearer_token=bearer,
        sessions_dir=cfg.memory.sessions_dir,
        prefs_path=prefs_path,
        log_level=cfg.server.log_level,
    )
    return cfg, bot_cfg

"""Entry point: ``python -m fitt_telegram_bot`` or the
``fitt-telegram-bot`` console script."""

from __future__ import annotations

import sys

from gateway.errors import ConfigError
from gateway.logging_config import configure_logging, get_logger

from .bot import build_application
from .config import load_bot_config


def main() -> int:
    try:
        cfg, bot_cfg = load_bot_config()
    except ConfigError as e:
        print(f"[fitt-telegram-bot] configuration error: {e}", file=sys.stderr)
        return 2

    configure_logging(
        cfg.logging.dir,
        level=cfg.server.log_level,
        retention_days=cfg.logging.retention_days,
        filename="telegram-bot.log",
    )
    log = get_logger("fitt.telegram_bot")

    if not bot_cfg.allowlist_user_ids:
        log.warning(
            "telegram.allowlist.empty",
            note="No Telegram users are allowlisted. The bot will drop every incoming message.",
        )

    app = build_application(bot_cfg)
    log.info("telegram.bot.starting", gateway=bot_cfg.gateway_url)
    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

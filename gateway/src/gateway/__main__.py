"""Entry point: ``python -m gateway`` or the ``fitt-gateway`` console script.

Loads config + secrets, configures logging, builds the FastAPI app,
and runs uvicorn.
"""

from __future__ import annotations

import sys

import uvicorn

from .app import create_app
from .config import default_config_path, default_secrets_path, load_config
from .errors import ConfigError
from .logging_config import configure_logging, get_logger


def main() -> int:
    try:
        cfg = load_config(default_config_path(), default_secrets_path())
    except ConfigError as e:
        print(f"[fitt-gateway] configuration error: {e}", file=sys.stderr)
        print(
            "[fitt-gateway] copy configs/config.example.yaml and "
            "configs/secrets.example.yaml into ~/.fitt/ and edit them.",
            file=sys.stderr,
        )
        return 2

    configure_logging(
        cfg.logging.dir,
        level=cfg.server.log_level,
        retention_days=cfg.logging.retention_days,
    )
    log = get_logger()
    log.info(
        "gateway.starting",
        host=cfg.server.host,
        port=cfg.server.port,
        aliases=cfg.alias_names(),
    )

    app = create_app(cfg)
    uvicorn.run(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=cfg.server.log_level,
        access_log=False,  # we do our own structured logs
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

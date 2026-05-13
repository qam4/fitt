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
        # Loud, structured stderr so operators running under docker
        # see the same shape regardless of context. The compose
        # restart policy is ``on-failure:3``: after three failed
        # boots, ``docker compose ps`` shows ``Exited (2)`` and
        # this stderr lives in ``docker compose logs fitt-gateway
        # --tail 30``. The trailing hint points at that command so
        # operators don't have to remember it.
        print("=" * 60, file=sys.stderr)
        print(f"[fitt-gateway] CONFIG ERROR (exit 2): {e}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        print(
            "[fitt-gateway] To fix: edit your config files in ~/.fitt/ "
            "(or whatever FITT_HOME points at).",
            file=sys.stderr,
        )
        print(
            "[fitt-gateway] Templates: configs/config.example.yaml + "
            "configs/secrets.example.yaml in the repo.",
            file=sys.stderr,
        )
        print(
            "[fitt-gateway] Under docker compose: re-run with "
            "`docker compose logs fitt-gateway --tail 30` to see "
            "this message after a `compose ps` shows Exited(2).",
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

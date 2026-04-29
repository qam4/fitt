"""Structured JSON logging for the gateway.

All logs go through ``structlog`` and end up as one JSON object per
line. Two sinks:

* Rotating file at ``<logging.dir>/gateway.log`` — the source of truth
  for ``fitt cost`` aggregation.
* Console (stderr) — human-friendly for dev / live debugging.

Request logs use the schema documented in design.md §
``gateway/logging_config.py``.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict, Processor, WrappedLogger

_CONFIGURED = False


def _json_safe(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Coerce non-JSON-native values (Decimal, Path) to strings/floats.

    We keep Decimal → str because float conversion would lose precision
    on cost values, and downstream tools (``fitt cost``) parse the
    string back into a Decimal.
    """
    for k, v in list(event_dict.items()):
        if isinstance(v, Decimal):
            event_dict[k] = str(v)
        elif isinstance(v, Path):
            event_dict[k] = str(v)
    return event_dict


def _build_processors(*, for_file: bool) -> list[Processor]:
    common: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _json_safe,
    ]
    if for_file:
        return [*common, structlog.processors.JSONRenderer()]
    return [*common, structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())]


def configure_logging(
    log_dir: Path,
    *,
    level: str = "info",
    retention_days: int = 30,
) -> None:
    """Configure structlog to write JSON to a rotating file + console.

    Idempotent: repeated calls are a no-op. Tests that need to
    reconfigure should call ``reset_logging()`` first.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "gateway.log"

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # stdlib-logging side: a rotating file handler (JSON) and a stderr
    # handler (human). structlog wraps these.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_path,
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(_StructlogFormatter(for_file=True))

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(_StructlogFormatter(for_file=False))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(numeric_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _json_safe,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def reset_logging() -> None:
    """Testing helper: clear configuration so ``configure_logging`` runs again."""
    global _CONFIGURED
    logging.getLogger().handlers.clear()
    structlog.reset_defaults()
    _CONFIGURED = False


class _StructlogFormatter(logging.Formatter):
    """Render structlog events through the appropriate processor chain."""

    def __init__(self, *, for_file: bool) -> None:
        super().__init__()
        self._processor = (
            structlog.processors.JSONRenderer()
            if for_file
            else structlog.dev.ConsoleRenderer(colors=False)
        )

    def format(self, record: logging.LogRecord) -> str:
        # record.msg is an EventDict when the log came from structlog.
        if isinstance(record.msg, dict):
            event_dict: dict[str, Any] = dict(record.msg)
        else:
            event_dict = {"event": record.getMessage()}
        event_dict.setdefault("level", record.levelname.lower())
        return self._processor(None, record.levelname, event_dict)  # type: ignore[arg-type]


def get_logger(name: str = "fitt.gateway") -> structlog.stdlib.BoundLogger:
    """Return a configured structlog logger."""
    return structlog.get_logger(name)


def log_request(logger: structlog.stdlib.BoundLogger, **fields: Any) -> None:
    """Emit one structured request log event.

    Caller is responsible for providing:
      * alias, model, backend, backend_actual
      * latency_ms, input_tokens, output_tokens, cost_usd
      * status ("ok" | "error" | "fallback")
    plus any error-specific fields.
    """
    logger.info("chat.completion", **fields)

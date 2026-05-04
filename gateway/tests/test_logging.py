"""Tests for the structured logging setup."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from gateway.logging_config import configure_logging, get_logger, log_request, reset_logging


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_logging()
    yield
    reset_logging()


def _read_one_json_line(log_path: Path) -> dict:
    text = log_path.read_text(encoding="utf-8").strip()
    assert text, f"log file {log_path} is empty"
    # Last line is the most recent event.
    last = text.splitlines()[-1]
    return json.loads(last)


def test_log_file_is_created(tmp_path: Path) -> None:
    configure_logging(tmp_path / "logs")
    logger = get_logger()
    logger.info("test.event", key="value")

    log_path = tmp_path / "logs" / "gateway.log"
    assert log_path.exists()


def test_log_line_is_valid_json(tmp_path: Path) -> None:
    configure_logging(tmp_path / "logs")
    logger = get_logger()
    logger.info("test.event", alias="fitt-smart", latency_ms=42)

    entry = _read_one_json_line(tmp_path / "logs" / "gateway.log")
    assert entry["event"] == "test.event"
    assert entry["alias"] == "fitt-smart"
    assert entry["latency_ms"] == 42
    assert "timestamp" in entry
    assert entry["level"] == "info"


def test_log_decimal_rendered_as_string(tmp_path: Path) -> None:
    """Decimal costs are logged as strings (no float lossiness)."""
    configure_logging(tmp_path / "logs")
    logger = get_logger()
    logger.info("test.event", cost_usd=Decimal("0.000587"))

    entry = _read_one_json_line(tmp_path / "logs" / "gateway.log")
    assert entry["cost_usd"] == "0.000587"


def test_log_request_emits_chat_completion_event(tmp_path: Path) -> None:
    configure_logging(tmp_path / "logs")
    logger = get_logger()
    log_request(
        logger,
        alias="fitt-smart",
        model="anthropic/claude-sonnet-4.5",
        backend="openrouter",
        backend_actual="openrouter",
        latency_ms=1420,
        input_tokens=532,
        output_tokens=284,
        cost_usd=Decimal("0.00586"),
        status="ok",
    )
    entry = _read_one_json_line(tmp_path / "logs" / "gateway.log")
    assert entry["event"] == "chat.completion"
    assert entry["alias"] == "fitt-smart"
    assert entry["cost_usd"] == "0.00586"
    assert entry["status"] == "ok"


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """Calling configure_logging twice doesn't stack handlers."""
    configure_logging(tmp_path / "logs")
    import logging as stdlib_logging

    n_handlers_after_first = len(stdlib_logging.getLogger().handlers)
    configure_logging(tmp_path / "logs")  # second call
    n_handlers_after_second = len(stdlib_logging.getLogger().handlers)

    assert n_handlers_after_first == n_handlers_after_second


def test_stdlib_logger_events_get_timestamped(tmp_path: Path) -> None:
    """Third-party libraries log via stdlib (uvicorn, httpx, LiteLLM).

    Before a fix this weekend, stdlib-originated events reached the
    file handler without a timestamp - structlog's TimeStamper only
    ran on structlog events. The formatter now runs its own chain
    at format time so every written line carries a timestamp.
    """
    import logging as stdlib_logging

    configure_logging(tmp_path / "logs")
    # Some earlier test in the full suite may have raised httpx's
    # logger level (respx or httpx itself does this). Force it back
    # to INFO so our single log call lands in the file.
    stdlib_logging.getLogger("httpx").setLevel(stdlib_logging.INFO)
    stdlib_logging.getLogger("httpx").info("HTTP Request: POST https://api.example.com/v1")

    entry = _read_one_json_line(tmp_path / "logs" / "gateway.log")
    assert "timestamp" in entry, entry
    assert entry["level"] == "info"
    assert "HTTP Request" in entry["event"]
    # The `logger` key lets readers tell stdlib-origin events apart
    # from the gateway's own structlog events.
    assert entry["logger"] == "httpx"


def test_structlog_events_retain_their_timestamp(tmp_path: Path) -> None:
    """TimeStamper is idempotent: a structlog event already has a
    timestamp when it reaches the formatter; the formatter's second
    TimeStamper pass must not double-stamp or overwrite it."""
    configure_logging(tmp_path / "logs")
    logger = get_logger()
    logger.info("test.event")

    entry = _read_one_json_line(tmp_path / "logs" / "gateway.log")
    ts = entry.get("timestamp")
    assert isinstance(ts, str) and ts
    # ISO-8601 UTC ends with Z or +00:00 depending on structlog version.
    assert "T" in ts, ts


def test_configure_logging_honors_filename(tmp_path: Path) -> None:
    """Caller can redirect writes to a custom filename inside log_dir.

    The Telegram bot passes `filename="telegram-bot.log"` so its
    events don't co-mingle with the gateway's.
    """
    configure_logging(tmp_path / "logs", filename="telegram-bot.log")
    logger = get_logger("fitt.telegram_bot")
    logger.info("bot.started")

    default_path = tmp_path / "logs" / "gateway.log"
    custom_path = tmp_path / "logs" / "telegram-bot.log"
    assert not default_path.exists(), "gateway.log must not be touched"
    assert custom_path.exists()
    entry = _read_one_json_line(custom_path)
    assert entry["event"] == "bot.started"

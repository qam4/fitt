"""The bot entry point must harden its streams before it prints.

It runs as a service with stdout captured to a file, and Telegram
content is full of non-ASCII — so on Windows an unhardened stream turns
a readable config error into a UnicodeEncodeError traceback.

The helper itself is tested in the gateway package (it lives there);
this asserts the wiring on this side. See
gateway/src/gateway/stdio_encoding.py for why the class recurs.
"""

from __future__ import annotations

from typing import Any

from gateway.errors import ConfigError

from fitt_telegram_bot import __main__ as bot_main


def test_entry_point_hardens_output_before_its_first_print(monkeypatch: Any) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(bot_main, "make_output_encoding_safe", lambda: calls.append(True))

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ConfigError("boom")

    monkeypatch.setattr(bot_main, "load_bot_config", _boom)

    assert bot_main.main() == 2
    assert calls, "fitt-telegram-bot prints a config error before fixing its streams"

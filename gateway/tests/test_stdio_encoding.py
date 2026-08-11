"""Every entry point must survive a non-UTF-8 stdout.

Live failure this guards: `fitt eval e2e` redirected to a file on Windows
ran all six scenarios, wrote the report, then raised UnicodeEncodeError
printing the report path (an arrow through a cp1252 stream) — which also
skipped the --min-objective-rate exit-code gate, so the wrapper script
reported a clean batch for a run that had crashed.

The per-entry-point tests below exist because fixing only the entry point
we noticed is what let this class recur: the CLI, the gateway service and
the bot service all print, and all three run with captured stdout.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import click
from click.testing import CliRunner

from gateway import cli
from gateway.errors import ConfigError
from gateway.stdio_encoding import make_output_encoding_safe


def test_non_ascii_survives_a_cp1252_stdout(monkeypatch: Any) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))

    make_output_encoding_safe()
    sys.stdout.write("report \u2192 /tmp/e2e.md\n")
    sys.stdout.flush()

    assert "\u2192".encode() in raw.getvalue()


def test_non_ascii_survives_a_cp1252_stderr(monkeypatch: Any) -> None:
    """Config errors and tracebacks go to stderr, which services capture."""
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(raw, encoding="cp1252"))

    make_output_encoding_safe()
    sys.stderr.write("alias \u2192 model mismatch\n")
    sys.stderr.flush()

    assert "\u2192".encode() in raw.getvalue()


def test_stream_without_reconfigure_is_left_alone(monkeypatch: Any) -> None:
    """Captured stdout (pytest, some CI runners) has no reconfigure()."""

    class _Plain(io.StringIO):
        reconfigure = None  # type: ignore[assignment]

    monkeypatch.setattr(sys, "stdout", _Plain())

    make_output_encoding_safe()  # must not raise

    sys.stdout.write("ok")
    assert sys.stdout.getvalue() == "ok"  # type: ignore[attr-defined]


def test_console_output_is_still_capturable(monkeypatch: Any) -> None:
    """The console must resolve stdout per write, not bind it at import.

    Binding a stream onto the rich Console breaks click's runner (and
    any other redirect) — which is how the first attempt at this fix
    failed, 51 tests deep.
    """
    calls: list[bool] = []
    monkeypatch.setattr(cli, "make_output_encoding_safe", lambda: calls.append(True))

    @cli.main.command("encoding-probe")
    def _probe() -> None:
        cli._console.print("captured \u2192 yes")

    try:
        result = CliRunner().invoke(cli.main, ["encoding-probe"])
    finally:
        assert isinstance(cli.main, click.Group)
        cli.main.commands.pop("encoding-probe", None)

    assert result.exit_code == 0, result.output
    assert "captured" in result.output
    assert calls, "the fitt CLI group callback no longer hardens its output"


def test_gateway_service_hardens_output_before_its_first_print(
    monkeypatch: Any,
) -> None:
    from gateway import __main__ as gateway_main

    calls: list[bool] = []
    monkeypatch.setattr(gateway_main, "make_output_encoding_safe", lambda: calls.append(True))
    monkeypatch.setattr(gateway_main, "load_config", _raise_config_error)

    assert gateway_main.main() == 2
    assert calls, "fitt-gateway prints a config error before fixing its streams"


# The third entry point, fitt-telegram-bot, is asserted the same way in
# telegram-bot/tests/test_stdio_encoding.py - that package isn't
# installed in this venv, so an importorskip here would only look like
# coverage.


def _raise_config_error(*_args: Any, **_kwargs: Any) -> Any:
    raise ConfigError("boom")

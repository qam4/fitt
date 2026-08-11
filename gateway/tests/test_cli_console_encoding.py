"""CLI output must survive a non-UTF-8 stdout.

Live failure this guards: `fitt eval e2e` redirected to a file on Windows
ran all six scenarios, wrote the report, then raised UnicodeEncodeError
printing the report path (an arrow through a cp1252 stream) — which also
skipped the --min-objective-rate exit-code gate, so the wrapper script
reported a clean batch for a run that had crashed.
"""

from __future__ import annotations

import io
import sys
from typing import Any

import click
from click.testing import CliRunner

from gateway import cli


def test_non_ascii_survives_a_cp1252_stream(monkeypatch: Any) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))

    cli._make_output_encoding_safe()
    sys.stdout.write("report \u2192 /tmp/e2e.md\n")
    sys.stdout.flush()

    assert "\u2192".encode() in raw.getvalue()


def test_stream_without_reconfigure_is_left_alone(monkeypatch: Any) -> None:
    """Captured stdout (pytest, some CI runners) has no reconfigure()."""

    class _Plain(io.StringIO):
        reconfigure = None  # type: ignore[assignment]

    monkeypatch.setattr(sys, "stdout", _Plain())

    cli._make_output_encoding_safe()  # must not raise

    sys.stdout.write("ok")
    assert sys.stdout.getvalue() == "ok"  # type: ignore[attr-defined]


def test_console_output_is_still_capturable() -> None:
    """The console must resolve stdout per write, not bind it at import.

    Binding a stream onto the Console breaks click's runner (and any
    other redirect), which is how the first attempt at this fix failed.
    """

    @cli.main.command("encoding-probe")
    def _probe() -> None:
        cli._console.print("captured \u2192 yes")

    try:
        result = CliRunner().invoke(cli.main, ["encoding-probe"])
        assert result.exit_code == 0, result.output
        assert "captured" in result.output
    finally:
        assert isinstance(cli.main, click.Group)
        cli.main.commands.pop("encoding-probe", None)

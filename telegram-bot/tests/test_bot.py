"""Tests for ``bot.build_application``.

The only thing worth unit-testing here is the concurrent-updates
configuration — everything else in ``build_application`` is PTB
plumbing that's either trivial or needs a live Telegram connection
to exercise.

The specific invariant we're pinning: PTB must be configured to
dispatch updates in parallel. Without that, a chat text handler
that awaits the gateway blocks EVERY other update on the same
bot, including the inline-keyboard callback whose tap is exactly
what the chat handler is waiting for. The approval middleware
times out at 45 seconds; the callback then runs, POSTs /decide,
and gets 404 because the approval was cleaned up. Every
ask-bucket tool fails that way. Diagnosed end-to-end in the
2026-05-07 debug session; the fix is a one-line
``concurrent_updates(4)`` on the ApplicationBuilder.

If a future refactor drops that call this test breaks, which is
the point — the symptom is "every approval deadlocks" and it's
invisible until you live-test, so the guard goes in unit tests.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from fitt_telegram_bot.bot import build_application
from fitt_telegram_bot.config import load_bot_config


def _seed_fitt_home(fitt_home: Path) -> None:
    """Minimal valid config+secrets so ``load_bot_config`` runs."""
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
              - name: personal
                token: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA

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


def test_build_application_enables_concurrent_updates(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PTB's default is strictly-serial update dispatch. That
    deadlocks the approval flow because the chat text handler
    awaits the gateway, which awaits the approval, which needs
    the callback handler to run — but the callback is blocked
    behind the chat handler.

    The fix is ``concurrent_updates(N)`` on ApplicationBuilder.
    Any positive N unblocks the deadlock; we assert > 0 rather
    than a specific number so tweaking the bound doesn't break
    the test for the wrong reason."""
    _seed_fitt_home(isolate_fitt_home)
    monkeypatch.delenv("FITT_GATEWAY_URL", raising=False)

    _cfg, bot_cfg = load_bot_config()
    app = build_application(bot_cfg)
    assert app.concurrent_updates > 0, (
        "Application is configured for serial update dispatch. "
        "Approval inline-keyboard callbacks will deadlock behind "
        "in-flight chat handlers. Re-enable via "
        "ApplicationBuilder().concurrent_updates(N)."
    )


def test_build_application_bounds_concurrency(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """We opt into concurrent updates with a bounded N rather than
    ``True`` (which is PTB's "unbounded" shorthand). A small
    explicit number limits fan-out if the gateway ever
    misbehaves and should be plenty for a single-user bot.

    Bumping the value in production code is fine; this test
    guards against accidentally flipping to unbounded."""
    _seed_fitt_home(isolate_fitt_home)
    monkeypatch.delenv("FITT_GATEWAY_URL", raising=False)

    _cfg, bot_cfg = load_bot_config()
    app = build_application(bot_cfg)
    # PTB exposes the bound as an int on the property; "unbounded"
    # would be represented as sys.maxsize or similar large value.
    # Anything above a few tens would be a flag that the intent
    # changed.
    assert 0 < app.concurrent_updates <= 32, (
        f"concurrent_updates={app.concurrent_updates}; expected "
        "a small bounded value. If you meant to raise the bound, "
        "update this guard with the rationale."
    )

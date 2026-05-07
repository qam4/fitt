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


# --------------------------------------------------------------- callback keyboard cleanup


async def test_approval_callback_clears_keyboard_on_success(
    isolate_fitt_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: after a successful approve/reject/trust,
    the prompt's inline keyboard must be removed so re-tapping
    the same message doesn't fire another decide request.

    Pre-fix, the edit passed only `text=`, leaving the keyboard
    on the message; users could tap the same button repeatedly
    and produce cascade 404s (seen live against prompts left
    over from earlier gateway restarts)."""
    import types
    from unittest.mock import AsyncMock, MagicMock

    from telegram import Message

    from fitt_telegram_bot.approval import build_callback_data
    from fitt_telegram_bot.bot import _on_approval_callback
    from fitt_telegram_bot.handlers import Services

    # Shape-compatible stand-ins. Using types.SimpleNamespace for
    # the query / user objects keeps the test dependency-free of
    # PTB's full Update/CallbackQuery machinery.
    approval_id = "11111111-2222-3333-4444-555555555555"
    callback_data = build_callback_data("approve", approval_id)

    query = types.SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(return_value=None),
        message=MagicMock(spec=Message),
    )
    query.message.text = "🔐 Tool approval needed\nTool: git_status\nArgs: {}"
    query.message.chat_id = 42
    query.message.message_id = 1001

    update = types.SimpleNamespace(
        callback_query=query,
        effective_user=types.SimpleNamespace(id=7, username="tester"),
    )

    gateway = types.SimpleNamespace(
        decide_approval=AsyncMock(return_value=(True, None)),
    )
    services = Services(
        gateway=gateway,  # type: ignore[arg-type]
        prefs=MagicMock(),
        sessions=MagicMock(),
        allowlist=frozenset({7}),
    )

    bot = types.SimpleNamespace(edit_message_text=AsyncMock(return_value=None))
    context = types.SimpleNamespace(
        bot=bot,
        bot_data={"services": services},
    )

    await _on_approval_callback(update, context)  # type: ignore[arg-type]

    # The decide POST ran.
    gateway.decide_approval.assert_awaited_once_with(approval_id, "approve")
    # The message got edited, and critically, reply_markup=None
    # cleared the keyboard. Without this, re-tapping the old
    # prompt cascades 404s.
    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == 42
    assert kwargs["message_id"] == 1001
    assert "✅" in kwargs["text"]
    assert kwargs["reply_markup"] is None


async def test_approval_callback_clears_keyboard_on_404(
    isolate_fitt_home: Path,
) -> None:
    """Same guard for the failure branch. A 404 from the gateway
    (stale approval id) leaves the message visible but the
    keyboard has to go — re-tapping would produce the same 404
    and dirty the bot's logs without making progress."""
    import types
    from unittest.mock import AsyncMock, MagicMock

    from telegram import Message

    from fitt_telegram_bot.approval import build_callback_data
    from fitt_telegram_bot.bot import _on_approval_callback
    from fitt_telegram_bot.handlers import Services

    approval_id = "stale-id-from-a-prior-gateway-process"
    callback_data = build_callback_data("approve", approval_id)

    query = types.SimpleNamespace(
        data=callback_data,
        answer=AsyncMock(return_value=None),
        message=MagicMock(spec=Message),
    )
    query.message.text = "🔐 Tool approval needed\nTool: old_tool"
    query.message.chat_id = 42
    query.message.message_id = 1002

    update = types.SimpleNamespace(
        callback_query=query,
        effective_user=types.SimpleNamespace(id=7, username="tester"),
    )

    gateway = types.SimpleNamespace(
        decide_approval=AsyncMock(
            return_value=(False, "HTTP 404: approval not found or already resolved")
        ),
    )
    services = Services(
        gateway=gateway,  # type: ignore[arg-type]
        prefs=MagicMock(),
        sessions=MagicMock(),
        allowlist=frozenset({7}),
    )

    bot = types.SimpleNamespace(edit_message_text=AsyncMock(return_value=None))
    context = types.SimpleNamespace(
        bot=bot,
        bot_data={"services": services},
    )

    await _on_approval_callback(update, context)  # type: ignore[arg-type]

    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.await_args.kwargs
    assert "⚠️" in kwargs["text"]
    assert "404" in kwargs["text"]
    assert kwargs["reply_markup"] is None

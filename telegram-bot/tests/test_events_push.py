"""Tests for the Telegram event-push formatter.

Phase 4.5 Task 5.5e scaffolding. The formatters themselves are
pure functions over dict shapes, so these tests drive them
directly — no PTB plumbing involved.
"""

from __future__ import annotations

from fitt_telegram_bot.events_push import _BODY_CAP, _TRUNCATED, format_event


def _evt(kind: str, **fields) -> dict:  # type: ignore[no-untyped-def]
    base = {
        "ts": 0.0,
        "kind": kind,
        "session_key": "main",
        "title": "",
        "body": "",
        "meta": {},
    }
    base.update(fields)
    return base


# --------------------------------------------------------------- late_tool_result


def test_format_late_tool_result_includes_tool_name_and_body() -> None:
    out = format_event(
        _evt(
            "late_tool_result",
            title="✅ late result: write_file",
            body="wrote a.txt. done.",
            meta={"tool": "write_file"},
        )
    )
    assert "write_file" in out
    assert "wrote a.txt" in out
    assert out.startswith("✅")


def test_format_late_tool_result_missing_tool_name_uses_fallback() -> None:
    out = format_event(_evt("late_tool_result", body="done", meta={}))
    # No tool name in meta → header says "tool".
    assert "Late result from tool" in out


# --------------------------------------------------------------- late_tool_rejected


def test_format_late_tool_rejected_warns_and_includes_body() -> None:
    out = format_event(
        _evt(
            "late_tool_rejected",
            body="user tapped reject; standing down.",
            meta={"tool": "write_file"},
        )
    )
    assert out.startswith("⚠️")
    assert "write_file" in out
    assert "standing down" in out


# --------------------------------------------------------------- body cap


def test_body_cap_truncates_with_sentinel() -> None:
    long_body = "x" * (_BODY_CAP * 2)
    out = format_event(
        _evt(
            "late_tool_result",
            body=long_body,
            meta={"tool": "write_file"},
        )
    )
    assert out.endswith(_TRUNCATED)
    # Output length must not exceed header + cap.
    # Header for late_tool_result is "✅ Late result from write_file" + blank line.
    assert len(out) <= _BODY_CAP + 100


def test_body_below_cap_passes_through() -> None:
    body = "a short reply"
    out = format_event(_evt("late_tool_result", body=body, meta={"tool": "t"}))
    assert _TRUNCATED not in out
    assert body in out


# --------------------------------------------------------------- cron + agent fallback


def test_cron_completed_uses_cron_name() -> None:
    out = format_event(
        _evt(
            "cron_completed",
            title="cron 'briefing'",
            body="nothing urgent.",
            meta={"cron_name": "briefing"},
        )
    )
    assert "briefing" in out
    assert "nothing urgent" in out
    assert out.startswith("✅")


def test_silent_cron_completed_returns_empty() -> None:
    """``silent: true`` crons land with an empty body. Returning
    a "✅ cron X" pingback every firing would defeat the flag.
    The push pipeline treats empty formatted output as "skip
    delivery" — so this is the silent path's off-switch."""
    out = format_event(_evt("cron_completed", title="cron 'monitor'", body="", meta={}))
    assert out == ""


def test_cron_failed_shows_error() -> None:
    out = format_event(
        _evt(
            "cron_failed",
            title="cron 'briefing' failed",
            body="upstream kaboom",
            meta={"cron_name": "briefing"},
        )
    )
    assert "briefing" in out and "failed" in out
    assert "upstream kaboom" in out
    assert out.startswith("❌")


def test_agent_message_plain_body() -> None:
    out = format_event(
        _evt("agent_message", title="Agent Message", body="hey, your build is done."),
    )
    # Default title is elided; body carries the message.
    assert out == "hey, your build is done."


def test_agent_message_custom_title_included() -> None:
    out = format_event(
        _evt("agent_message", title="Build Notice", body="ok"),
    )
    assert out.startswith("Build Notice")
    assert "ok" in out


def test_unknown_kind_falls_back_to_title_and_body() -> None:
    out = format_event(_evt("unknown_future_kind", title="something happened", body="details"))
    assert "something happened" in out
    assert "details" in out


def test_empty_body_and_empty_title_produce_empty_string_for_unknown_kind() -> None:
    # Unknown kind with neither title nor body produces just the
    # kind name itself so the operator sees *something*.
    out = format_event(_evt("late_unknown"))
    assert out == "late_unknown"

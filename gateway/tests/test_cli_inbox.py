"""Tests for ``fitt inbox`` — Phase 4.5 Task 9.

The CLI opens ``$FITT_HOME/events.jsonl`` directly via
:class:`EventLog` and prints matching entries. Filters are
``--since``, ``--kind``, ``--session``, ``--limit``; ``--json``
emits JSON lines for scripting.
"""

from __future__ import annotations

import json
from pathlib import Path
from time import time

from click.testing import CliRunner

from gateway.cli import main as fitt_cli
from gateway.events import EventLog, default_events_path, new_entry


def _seed(fitt_home: Path) -> EventLog:
    """Write a small corpus covering the kinds the CLI cares
    about. Returns the log so tests can inspect it.

    Seeded in chronological order because :meth:`EventLog.read`
    honours file order for the ``limit`` slice (it keeps the
    tail). Out-of-order appends would make ``--limit`` slice
    the wrong end.
    """
    log = EventLog(default_events_path(fitt_home))
    now = time()
    # Oldest first — a stale event outside the default --since 24h.
    log.append(
        new_entry(
            ts=now - 10 * 86400,
            kind="cron_completed",
            session_key="cron:old:1",
            title="old firing",
            body="stale content",
        )
    )
    log.append(
        new_entry(
            ts=now - 3600,
            kind="cron_fired",
            session_key="cron:abc:1",
            title="cron 'briefing'",
        )
    )
    log.append(
        new_entry(
            ts=now - 3500,
            kind="cron_completed",
            session_key="cron:abc:1",
            title="briefing",
            body="Nothing urgent.",
        )
    )
    # Newest last.
    log.append(
        new_entry(
            ts=now - 60,
            kind="agent_message",
            session_key="main",
            title="Reminder",
            body="Don't forget dinner.",
        )
    )
    return log


def test_inbox_default_shows_recent_human_readable(isolate_fitt_home: Path) -> None:
    _seed(isolate_fitt_home)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["inbox"])
    assert result.exit_code == 0, result.output
    assert "cron_completed" in result.output
    assert "briefing" in result.output
    assert "agent_message" in result.output
    assert "Reminder" in result.output
    # The 10-day-old event is older than the default --since 24h.
    assert "old firing" not in result.output


def test_inbox_since_7d_includes_stale(isolate_fitt_home: Path) -> None:
    _seed(isolate_fitt_home)
    runner = CliRunner()
    # 7d still misses 10-day-old → ensure --since 14d does see it.
    result = runner.invoke(fitt_cli, ["inbox", "--since", "14d"])
    assert result.exit_code == 0, result.output
    assert "old firing" in result.output


def test_inbox_kind_filter(isolate_fitt_home: Path) -> None:
    _seed(isolate_fitt_home)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["inbox", "--kind", "agent_message"])
    assert result.exit_code == 0, result.output
    assert "agent_message" in result.output
    # cron_completed and cron_fired should be filtered out.
    assert "cron_completed" not in result.output
    assert "cron_fired" not in result.output


def test_inbox_session_filter(isolate_fitt_home: Path) -> None:
    _seed(isolate_fitt_home)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["inbox", "--session", "main"])
    assert result.exit_code == 0, result.output
    assert "agent_message" in result.output
    assert "cron_fired" not in result.output


def test_inbox_limit(isolate_fitt_home: Path) -> None:
    _seed(isolate_fitt_home)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["inbox", "--since", "14d", "--limit", "1"])
    assert result.exit_code == 0, result.output
    # --limit 1 keeps the most recent → agent_message.
    assert "agent_message" in result.output
    assert "cron_fired" not in result.output


def test_inbox_json_emits_one_line_per_event(isolate_fitt_home: Path) -> None:
    _seed(isolate_fitt_home)
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["inbox", "--json"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.strip().splitlines() if line.strip()]
    decoded = [json.loads(line) for line in lines]
    # Default --since is 24h so only 3 of the 4 seeded events
    # are within range.
    kinds = [e["kind"] for e in decoded]
    assert "agent_message" in kinds
    assert "cron_completed" in kinds
    # Each entry has the expected top-level keys.
    for e in decoded:
        assert {"ts", "kind", "session_key", "title", "body", "meta"} <= set(e)


def test_inbox_empty_reports_no_events(isolate_fitt_home: Path) -> None:
    """No seeded events → the CLI prints a muted "(no events...)"
    message, not a table with zero rows."""
    runner = CliRunner()
    result = runner.invoke(fitt_cli, ["inbox"])
    assert result.exit_code == 0, result.output
    assert "no events" in result.output.lower()

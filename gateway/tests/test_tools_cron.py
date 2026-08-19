"""Tests for Phase 4.5 Task 3 — cron inline tools.

Each tool gets one happy-path test and one failure-mode test
(missing service, bad schedule, unknown id). The CronService
itself is already exhaustively tested in test_cron.py, so these
tests focus on what the tool layer adds: argument validation,
pretty-printing, and surfacing service errors as tool errors
the model can reason about.

The tool-facing arg is ``text`` (aligned with send_message /
learn_add, 2026-07-01); it maps to the internal ``CronJob.message``
field, which is why ``job.message`` assertions stay.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from gateway.cron import CronService
from gateway.projects import ProjectRegistry
from gateway.tools import Tool, ToolContext, build_cron_tools

# --------------------------------------------------------------- fixtures


def _tools() -> dict[str, Tool]:
    """Index the cron tools by name for one-line lookup."""
    return {t.name: t for t in build_cron_tools()}


def _ctx(cron: CronService | None) -> ToolContext:
    return ToolContext(
        client="telegram",
        session_key="main",
        projects=ProjectRegistry(Path("nonexistent.yaml")),
        cron=cron,
    )


@pytest.fixture
def svc(tmp_path: Path) -> CronService:
    return CronService(tmp_path / "cron.json")


# --------------------------------------------------------------- cron_add


async def test_cron_add_creates_every_schedule(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "ping monitor",
            "text": "check if host is up",
            "schedule_spec": "every 60s",
        },
        _ctx(svc),
    )
    assert not result.is_error
    jobs = svc.list()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.name == "ping monitor"
    assert job.message == "check if host is up"
    assert job.schedule.kind == "every"
    assert job.schedule.every_secs == 60
    # client + session are captured from the context.
    assert job.created_by_client == "telegram"
    assert job.session_key == "main"


async def test_cron_add_cron_expression_with_tz(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "briefing",
            "text": "summarise my PRs",
            "schedule_spec": "cron 0 8 * * 1-5",
            "timezone": "America/Los_Angeles",
        },
        _ctx(svc),
    )
    assert not result.is_error
    job = svc.list()[0]
    assert job.schedule.kind == "cron"
    assert job.schedule.cron_expr == "0 8 * * 1-5"
    assert job.schedule.timezone == "America/Los_Angeles"


async def test_cron_add_silent_polling_warns_about_approval(svc: CronService) -> None:
    """Interval + silent + default approval is almost always a
    user mistake — they'll get spammed with approval prompts even
    though they asked for silence. The tool notes this in its
    success payload."""
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "monitor",
            "text": "check",
            "schedule_spec": "every 60s",
            "silent": True,
            "approval_mode": "",
        },
        _ctx(svc),
    )
    assert not result.is_error
    assert "approval_mode='auto'" in result.payload


async def test_cron_add_no_warning_when_auto(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "monitor",
            "text": "check",
            "schedule_spec": "every 60s",
            "silent": True,
            "approval_mode": "auto",
        },
        _ctx(svc),
    )
    assert not result.is_error
    assert "approval_mode='auto'" not in result.payload


async def test_cron_add_rejects_bad_schedule(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "bad",
            "text": "m",
            "schedule_spec": "tomorrow at noon",
        },
        _ctx(svc),
    )
    assert result.is_error
    assert "invalid schedule" in result.payload


async def test_cron_add_name_optional_derived_from_text(svc: CronService) -> None:
    """Regression for the 2026-06-08 reminder failure: ``name``
    used to be required, and a required field literally called
    ``name`` (colliding with the function name) made small models
    thrash — hermes3:8b couldn't set a plain reminder. ``name`` is
    now optional and derived from the text; only ``text`` +
    ``schedule_spec`` are required."""
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "text": "Take out the trash tonight",
            "schedule_spec": "at 2026-06-08T20:00:00-04:00",
        },
        _ctx(svc),
    )
    assert not result.is_error, result.payload
    jobs = svc.list(include_disabled=True)
    assert len(jobs) == 1
    assert jobs[0].name == "Take out the trash tonight"


async def test_cron_add_still_requires_text(svc: CronService) -> None:
    """text is the one field that can't be derived."""
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {"name": "label", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    assert result.is_error
    assert "'text'" in result.payload


async def test_cron_add_rejects_invalid_approval_mode(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "n",
            "text": "m",
            "schedule_spec": "every 60",
            "approval_mode": "yolo",
        },
        _ctx(svc),
    )
    assert result.is_error
    assert "approval_mode" in result.payload


async def test_tool_fails_when_cron_service_missing() -> None:
    """If the service isn't wired (e.g. a misbuilt test app),
    the tool fails with a readable error, not AttributeError."""
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {"name": "n", "text": "m", "schedule_spec": "every 60"},
        _ctx(None),
    )
    assert result.is_error
    assert "cron service not available" in result.payload


# --------------------------------------------------------------- cron_list


async def test_cron_list_empty(svc: CronService) -> None:
    tool = _tools()["cron_list"]
    result = await tool.callable({}, _ctx(svc))
    assert not result.is_error
    assert "no crons" in result.payload


async def test_cron_list_formats_active_and_disabled(svc: CronService) -> None:
    add = _tools()["cron_add"]
    await add.callable(
        {"name": "one", "text": "m", "schedule_spec": "every 5m"},
        _ctx(svc),
    )
    j2 = (
        await add.callable(
            {"name": "two", "text": "m", "schedule_spec": "cron 0 9 * * *"},
            _ctx(svc),
        ),
    )
    del j2  # only need the side effect
    # Disable the second one via the pause tool.
    pause = _tools()["cron_pause"]
    target = svc.list()[1]
    await pause.callable({"id": target.id}, _ctx(svc))

    list_tool = _tools()["cron_list"]
    out = await list_tool.callable({"include_disabled": True}, _ctx(svc))
    assert not out.is_error
    assert "active" in out.payload
    assert "disabled" in out.payload
    assert "every 5m" in out.payload
    assert "cron 0 9 * * *" in out.payload


async def test_cron_list_can_hide_disabled(svc: CronService) -> None:
    add = _tools()["cron_add"]
    pause = _tools()["cron_pause"]
    list_tool = _tools()["cron_list"]

    await add.callable(
        {"name": "on", "text": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    await add.callable(
        {"name": "off", "text": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    target = svc.list()[1]
    await pause.callable({"id": target.id}, _ctx(svc))

    out = await list_tool.callable({"include_disabled": False}, _ctx(svc))
    assert "'on'" in out.payload
    assert "'off'" not in out.payload


# --------------------------------------------------------------- cron_update


async def test_cron_update_changes_fields(svc: CronService) -> None:
    add = _tools()["cron_add"]
    update = _tools()["cron_update"]

    await add.callable(
        {"name": "n", "text": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    job_id = svc.list()[0].id

    result = await update.callable(
        {
            "id": job_id,
            "name": "renamed",
            "silent": True,
            "schedule_spec": "every 5m",
        },
        _ctx(svc),
    )
    assert not result.is_error

    j = svc.get(job_id)
    assert j is not None
    assert j.name == "renamed"
    assert j.silent is True
    assert j.schedule.every_secs == 300


async def test_cron_update_requires_at_least_one_field(svc: CronService) -> None:
    add = _tools()["cron_add"]
    update = _tools()["cron_update"]
    await add.callable(
        {"name": "n", "text": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    job_id = svc.list()[0].id
    result = await update.callable({"id": job_id}, _ctx(svc))
    assert result.is_error
    assert "nothing to update" in result.payload


async def test_cron_update_unknown_id(svc: CronService) -> None:
    update = _tools()["cron_update"]
    result = await update.callable({"id": "nope", "name": "x"}, _ctx(svc))
    assert result.is_error
    assert "nope" in result.payload


# --------------------------------------------------------------- cron_remove


async def test_cron_remove(svc: CronService) -> None:
    add = _tools()["cron_add"]
    remove = _tools()["cron_remove"]
    await add.callable(
        {"name": "n", "text": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    job_id = svc.list()[0].id
    result = await remove.callable({"id": job_id}, _ctx(svc))
    assert not result.is_error
    assert svc.list() == []

    # Second remove is an error (idempotent at the service layer;
    # surfaced as an error here so the model can tell "actually
    # removed" from "was already gone").
    again = await remove.callable({"id": job_id}, _ctx(svc))
    assert again.is_error


# --------------------------------------------------------------- pause/resume


async def test_cron_pause_and_resume(svc: CronService) -> None:
    add = _tools()["cron_add"]
    pause = _tools()["cron_pause"]
    resume = _tools()["cron_resume"]
    await add.callable(
        {"name": "n", "text": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    job_id = svc.list()[0].id

    paused = await pause.callable({"id": job_id}, _ctx(svc))
    assert not paused.is_error
    j = svc.get(job_id)
    assert j is not None and j.enabled is False

    resumed = await resume.callable({"id": job_id}, _ctx(svc))
    assert not resumed.is_error
    j2 = svc.get(job_id)
    assert j2 is not None and j2.enabled is True


async def test_cron_pause_unknown_id(svc: CronService) -> None:
    pause = _tools()["cron_pause"]
    result = await pause.callable({"id": "nope"}, _ctx(svc))
    assert result.is_error


# --------------------------------------------------------------- schemas


def test_all_cron_tools_are_registered_with_expected_buckets() -> None:
    """Pin the approval buckets from the design doc so a future
    edit can't silently downgrade cron_remove to auto."""
    from gateway.tools import ApprovalBucket

    by_name = {t.name: t for t in build_cron_tools()}
    assert by_name["cron_add"].default_bucket is ApprovalBucket.ASK
    assert by_name["cron_update"].default_bucket is ApprovalBucket.ASK
    assert by_name["cron_remove"].default_bucket is ApprovalBucket.ASK
    assert by_name["cron_list"].default_bucket is ApprovalBucket.AUTO
    assert by_name["cron_pause"].default_bucket is ApprovalBucket.AUTO
    assert by_name["cron_resume"].default_bucket is ApprovalBucket.AUTO


def test_tool_schemas_reject_unknown_fields() -> None:
    """Schemas use additionalProperties: false so a typo in the
    model's tool_call payload produces a validation error instead
    of being silently ignored. Enforce that invariant."""
    for tool in build_cron_tools():
        extras = tool.schema.get("additionalProperties")
        assert extras is False, f"{tool.name} should reject extra properties"


def test_every_tool_has_description_and_schema() -> None:
    for tool in build_cron_tools():
        assert tool.description
        assert "type" in tool.schema
        _: Any = tool.schema  # silence mypy


def test_cron_add_uses_text_arg_not_message() -> None:
    """Regression guard for the 2026-07-01 rename: the tool-facing
    payload arg is ``text`` (aligned with send_message / learn_add),
    not ``message`` — the tool-consistency lint depends on this."""
    schema = _tools()["cron_add"].schema
    assert "text" in schema["properties"]
    assert "message" not in schema["properties"]
    assert "text" in schema["required"]


# ------------------------------------------- schedule confirmation
#
# A schedule the user can't verify is one they find out about by missing
# it. FITT already had the parsing half of this bug ("remind me at 1 PM"
# became 13:00 UTC and fired immediately); the [Current time] preamble
# fixed that. Confirmation stayed broken: a live run replied "I've
# scheduled a reminder ... for 15 minutes from now" with no absolute time.
# Found again by a requirements review, 2026-08-17.


async def test_a_one_shot_result_asks_the_model_to_state_the_local_time() -> None:
    from gateway.cron import CronSchedule
    from gateway.tools.cron_tools import _confirmation_hint

    # 2026-08-17 21:57 UTC, the timestamp from the live run.
    hint = _confirmation_hint(CronSchedule(kind="at", at_ts=1_787_003_858.0))

    assert "Tell the user this fires" in hint
    assert "check it's what they meant" in hint
    # It must name a concrete instant. (Not "'in 15 minutes' not in hint" —
    # the hint quotes that phrase as an example of what NOT to say, which
    # made the first version of this assertion a false negative.)
    assert re.search(r"\d{2}:\d{2}", hint), hint


async def test_the_hint_renders_in_local_time_not_utc() -> None:
    """UTC ISO was already in the result and got ignored; a human-readable
    local rendering is the point."""
    from datetime import datetime

    from gateway.cron import CronSchedule
    from gateway.tools.cron_tools import _confirmation_hint

    ts = 1_787_003_858.0
    expected = datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")

    assert expected in _confirmation_hint(CronSchedule(kind="at", at_ts=ts))


async def test_intervals_and_cron_expressions_get_no_hint() -> None:
    """ "every 2h" is self-describing — there's no single instant to
    misread, so don't pad every result with boilerplate."""
    from gateway.cron import CronSchedule
    from gateway.tools.cron_tools import _confirmation_hint

    assert _confirmation_hint(CronSchedule(kind="every", every_secs=7200)) == ""
    assert _confirmation_hint(CronSchedule(kind="cron", cron_expr="0 9 * * *")) == ""
    # Defensive: an 'at' with no timestamp must not raise.
    assert _confirmation_hint(CronSchedule(kind="at", at_ts=None)) == ""


async def test_cron_add_result_carries_the_confirmation(svc: CronService) -> None:
    """End to end through the tool, since the hint is only useful if it
    actually reaches the model's tool result."""
    res = await _tools()["cron_add"].callable(
        {"text": "Remind me to check my emails", "schedule_spec": "in 15 minutes"},
        _ctx(svc),
    )

    assert not res.is_error
    assert "Tell the user this fires" in res.payload


async def test_an_interval_cron_result_has_no_confirmation_noise(svc: CronService) -> None:
    res = await _tools()["cron_add"].callable(
        {"text": "check the build", "schedule_spec": "every 2h"}, _ctx(svc)
    )

    assert not res.is_error
    assert "Tell the user this fires" not in res.payload


# --------------------------------------------------------------- extra_tools grants
#
# Firings run on a reduced tool surface (see cron_runner.FIRING_DEFAULT_TOOLS).
# `extra_tools` is how a cron that genuinely needs a shell or the network says
# so. These pin the tool-layer half: parsing, persistence, and visibility.


async def test_cron_add_stores_extra_tools(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "text": "Check whether the build is green and tell me.",
            "schedule_spec": "every 30m",
            "extra_tools": ["project_shell"],
        },
        _ctx(svc),
    )
    assert not result.is_error, result.payload
    assert svc.list()[0].extra_tools == ["project_shell"]


async def test_cron_add_defaults_to_no_grant(svc: CronService) -> None:
    """A plain reminder must not acquire a surface by accident."""
    tool = _tools()["cron_add"]
    await tool.callable(
        {"text": "Remind me to check my emails", "schedule_spec": "in 15 minutes"},
        _ctx(svc),
    )
    assert svc.list()[0].extra_tools == []


async def test_cron_add_accepts_a_bare_string_grant(svc: CronService) -> None:
    """Models routinely send a scalar for an array-typed field. Refusing it
    costs a turn and teaches nothing; coercing is the same call the todowrite
    fumble-trap fix made."""
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "text": "Poll the deploy and tell me",
            "schedule_spec": "every 10m",
            "extra_tools": "http_get",
        },
        _ctx(svc),
    )
    assert not result.is_error, result.payload
    assert svc.list()[0].extra_tools == ["http_get"]


async def test_cron_add_rejects_a_malformed_grant_rather_than_dropping_it(
    svc: CronService,
) -> None:
    """A grant that silently didn't apply is worse than a failed call: the
    firing would fail later for a reason the model can't connect back."""
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "text": "Poll the deploy",
            "schedule_spec": "every 10m",
            "extra_tools": [{"name": "http_get"}],
        },
        _ctx(svc),
    )
    assert result.is_error
    assert "extra_tools" in result.payload


async def test_cron_update_replaces_the_grant_list(svc: CronService) -> None:
    """Replacement, not merge — otherwise revoking is impossible."""
    add = _tools()["cron_add"]
    update = _tools()["cron_update"]
    await add.callable(
        {
            "text": "Poll the deploy",
            "schedule_spec": "every 10m",
            "extra_tools": ["http_get", "project_shell"],
        },
        _ctx(svc),
    )
    job_id = svc.list()[0].id

    result = await update.callable({"id": job_id, "extra_tools": ["http_get"]}, _ctx(svc))
    assert not result.is_error, result.payload
    job = svc.get(job_id)
    assert job is not None and job.extra_tools == ["http_get"]


async def test_cron_list_shows_grants(svc: CronService) -> None:
    """ "What can this unattended job reach" is the audit question, and it is
    otherwise invisible without opening cron.json."""
    add = _tools()["cron_add"]
    await add.callable(
        {
            "text": "Poll the deploy",
            "schedule_spec": "every 10m",
            "extra_tools": ["project_shell"],
        },
        _ctx(svc),
    )
    out = await _tools()["cron_list"].callable({}, _ctx(svc))
    assert "project_shell" in out.payload


async def test_extra_tools_survives_a_reload_from_disk(svc: CronService, tmp_path: Path) -> None:
    """The grant lives in cron.json; a gateway restart must not widen or
    narrow it."""
    await _tools()["cron_add"].callable(
        {
            "text": "Poll the deploy",
            "schedule_spec": "every 10m",
            "extra_tools": ["http_get"],
        },
        _ctx(svc),
    )
    reopened = CronService(tmp_path / "cron.json")
    assert reopened.list()[0].extra_tools == ["http_get"]


def test_a_cron_written_before_grants_existed_loads_with_no_grant(
    tmp_path: Path,
) -> None:
    """Every cron on disk before 2026-08-19 lacks the field. Absent must
    read as "no grant" — the safe direction — and must not fail the load."""
    path = tmp_path / "cron.json"
    path.write_text(
        '{"jobs": [{"id": "old1", "name": "n", "message": "m", '
        '"schedule": {"kind": "every", "every_secs": 60}}]}',
        encoding="utf-8",
    )
    svc = CronService(path)
    jobs = svc.list()
    assert len(jobs) == 1, f"an old cron failed to load: {jobs}"
    assert jobs[0].extra_tools == []

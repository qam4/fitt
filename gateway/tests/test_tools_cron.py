"""Tests for Phase 4.5 Task 3 — cron inline tools.

Each tool gets one happy-path test and one failure-mode test
(missing service, bad schedule, unknown id). The CronService
itself is already exhaustively tested in test_cron.py, so these
tests focus on what the tool layer adds: argument validation,
pretty-printing, and surfacing service errors as tool errors
the model can reason about.
"""

from __future__ import annotations

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
            "message": "check if host is up",
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
            "message": "summarise my PRs",
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
            "message": "check",
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
            "message": "check",
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
            "message": "m",
            "schedule_spec": "tomorrow at noon",
        },
        _ctx(svc),
    )
    assert result.is_error
    assert "invalid schedule" in result.payload


async def test_cron_add_requires_name(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {"name": "", "message": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    assert result.is_error
    assert "'name'" in result.payload


async def test_cron_add_rejects_invalid_approval_mode(svc: CronService) -> None:
    tool = _tools()["cron_add"]
    result = await tool.callable(
        {
            "name": "n",
            "message": "m",
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
        {"name": "n", "message": "m", "schedule_spec": "every 60"},
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
        {"name": "one", "message": "m", "schedule_spec": "every 5m"},
        _ctx(svc),
    )
    j2 = (
        await add.callable(
            {"name": "two", "message": "m", "schedule_spec": "cron 0 9 * * *"},
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
        {"name": "on", "message": "m", "schedule_spec": "every 60"},
        _ctx(svc),
    )
    await add.callable(
        {"name": "off", "message": "m", "schedule_spec": "every 60"},
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
        {"name": "n", "message": "m", "schedule_spec": "every 60"},
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
        {"name": "n", "message": "m", "schedule_spec": "every 60"},
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
        {"name": "n", "message": "m", "schedule_spec": "every 60"},
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
        {"name": "n", "message": "m", "schedule_spec": "every 60"},
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

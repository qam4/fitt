"""Cron inline tools — ``cron_add`` / ``cron_list`` / ``cron_update``
/ ``cron_remove`` / ``cron_pause`` / ``cron_resume``.

These let the agent itself schedule, inspect, and cancel crons
via normal tool calls. Same pattern as the other inline tools:
the tool validates arguments, looks up the ``CronService`` off
the :class:`ToolContext`, delegates, and returns a string
payload.

Default buckets (matches the design doc):

* ``cron_list`` / ``cron_pause`` / ``cron_resume`` — ``auto``
  (inspecting and temporarily disabling crons is low-risk).
* ``cron_add`` / ``cron_update`` / ``cron_remove`` — ``ask``
  (scheduling future work and deleting records deserve a human
  confirmation).

The CronService does the real work (persistence, validation);
this module is mostly argument shaping + pretty-printing for the
model and for users reading the result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..cron import (
    ApprovalModeOverride,
    CronError,
    CronJob,
    CronSchedule,
    DuplicateCron,
    UnknownCron,
    parse_schedule_spec,
)
from ._types import ApprovalBucket, Tool, ToolContext, ToolResult

# --------------------------------------------------------------- schemas

_SCHEDULE_SPEC_ARG = {
    "type": "string",
    "description": (
        "When the cron should fire. Accepts: 'every N[unit]' "
        "(e.g. 'every 60s', 'every 5m', 'every 2h'); 'in N unit' "
        "(e.g. 'in 30 minutes'); 'at <iso|epoch>' (e.g. "
        "'at 2026-05-06T09:00:00-04:00' — PREFER timezone-aware "
        "ISO strings using the UTC offset from [Current time]; "
        "naive timestamps are interpreted as UTC which is rarely "
        "what the user means); 'cron <5-field>' (e.g. "
        "'cron 0 9 * * 1-5')."
    ),
}

_CRON_ID_ARG = {
    "type": "string",
    "description": "Short hex id of an existing cron (from cron_list).",
}

_SCHEMA_CRON_ADD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Human-readable label (e.g. 'morning briefing').",
        },
        "message": {
            "type": "string",
            "description": (
                "The prompt submitted to a fresh agent session when "
                "this cron fires. Should read like a self-contained "
                "user turn (e.g. 'List my open PRs and summarise.')."
            ),
        },
        "schedule_spec": _SCHEDULE_SPEC_ARG,
        "silent": {
            "type": "boolean",
            "description": (
                "When true, the final agent reply is NOT auto-"
                "delivered. Use for polling crons where you only "
                "want to hear on a state change — the agent is "
                "expected to call send_message explicitly."
            ),
            "default": False,
        },
        "approval_mode": {
            "type": "string",
            "enum": ["", "auto"],
            "description": (
                "When 'auto', tool calls inside this cron's "
                "firings auto-approve even if their default "
                "bucket is 'ask'. Essential for unattended "
                "polling crons — otherwise each firing sends an "
                "approval prompt."
            ),
            "default": "",
        },
        "agent_alias": {
            "type": "string",
            "description": (
                "Which model alias to use when the cron fires "
                "(e.g. 'fitt-smart' for cloud, 'fitt-default' "
                "for local). Empty string uses the gateway's "
                "cron default (fitt-default — the operator's "
                "everyday alias). Override to fitt-smart "
                "per-cron when you want better tool-calling "
                "reliability for unattended firings, or pin "
                "to fitt-default explicitly when cost matters."
            ),
            "default": "",
        },
        "timezone": {
            "type": "string",
            "description": (
                "IANA timezone name (e.g. 'America/Los_Angeles'). "
                "Applied to cron-expression schedules. Empty "
                "defaults to UTC."
            ),
            "default": "UTC",
        },
    },
    "required": ["name", "message", "schedule_spec"],
    "additionalProperties": False,
}

_SCHEMA_CRON_LIST: dict[str, Any] = {
    "type": "object",
    "properties": {
        "include_disabled": {
            "type": "boolean",
            "description": "Include paused crons in the output.",
            "default": True,
        },
    },
    "additionalProperties": False,
}

_SCHEMA_CRON_ID_ONLY: dict[str, Any] = {
    "type": "object",
    "properties": {"id": _CRON_ID_ARG},
    "required": ["id"],
    "additionalProperties": False,
}

_SCHEMA_CRON_UPDATE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": _CRON_ID_ARG,
        "name": {"type": "string"},
        "message": {"type": "string"},
        "schedule_spec": _SCHEDULE_SPEC_ARG,
        "silent": {"type": "boolean"},
        "approval_mode": {"type": "string", "enum": ["", "auto"]},
        "agent_alias": {"type": "string"},
        "timezone": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- helpers


def _get_cron_service(ctx: ToolContext) -> Any:
    """Fail readably when the service isn't wired. Better to see
    'cron service not available' than an AttributeError deep in
    the tool loop."""
    svc = ctx.cron
    if svc is None:
        return None
    return svc


def _format_schedule(sched: CronSchedule) -> str:
    """Human-readable one-liner for the list output."""
    if sched.kind == "every":
        n = sched.every_secs or 0
        if n % 3600 == 0:
            return f"every {n // 3600}h"
        if n % 60 == 0:
            return f"every {n // 60}m"
        return f"every {n}s"
    if sched.kind == "at":
        if sched.at_ts is None:
            return "at <unset>"
        dt = datetime.fromtimestamp(sched.at_ts, tz=UTC)
        return f"at {dt.isoformat()}"
    if sched.kind == "cron":
        tz_suffix = f" [{sched.timezone}]" if sched.timezone and sched.timezone != "UTC" else ""
        return f"cron {sched.cron_expr}{tz_suffix}"
    return f"<unknown kind: {sched.kind}>"


def _format_job(job: CronJob) -> str:
    bits = [
        job.id,
        "disabled" if not job.enabled else "active",
        _format_schedule(job.schedule),
    ]
    if job.silent:
        bits.append("silent")
    if job.approval_mode == "auto":
        bits.append("auto-approve")
    label = " ".join(bits)
    return f"- {label}  {job.name!r}"


# --------------------------------------------------------------- implementations


async def _tool_cron_add(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    svc = _get_cron_service(ctx)
    if svc is None:
        return ToolResult.error("cron service not available on this gateway")

    name = args.get("name")
    message = args.get("message")
    schedule_spec = args.get("schedule_spec")
    if not isinstance(name, str) or not name.strip():
        return ToolResult.error("'name' is required and must be non-empty")
    if not isinstance(message, str) or not message.strip():
        return ToolResult.error("'message' is required and must be non-empty")
    if not isinstance(schedule_spec, str) or not schedule_spec.strip():
        return ToolResult.error("'schedule_spec' is required")

    tz = str(args.get("timezone") or "UTC")
    try:
        schedule = parse_schedule_spec(schedule_spec, tz=tz)
    except CronError as e:
        return ToolResult.error(f"invalid schedule: {e}")

    silent = bool(args.get("silent", False))
    approval_mode_raw = args.get("approval_mode") or ""
    if approval_mode_raw not in ("", "auto"):
        return ToolResult.error("approval_mode must be '' or 'auto'")
    # At this point the value is one of the two Literals; the
    # annotation below makes mypy see that.
    approval_mode: ApprovalModeOverride = approval_mode_raw  # type: ignore[assignment]
    agent_alias = str(args.get("agent_alias") or "")

    job = CronJob(
        id="",
        name=name.strip(),
        message=message.strip(),
        schedule=schedule,
        silent=silent,
        approval_mode=approval_mode,
        agent_alias=agent_alias,
        session_key=ctx.session_key,
        created_by_client=ctx.client,
    )
    try:
        stored = svc.add(job)
    except DuplicateCron as e:
        return ToolResult.error(str(e))
    except CronError as e:
        return ToolResult.error(str(e))

    warning = ""
    if silent and approval_mode != "auto" and schedule.kind == "every":
        # A polling cron that's silent but not auto-approved
        # will ping the user for approval on every firing.
        # That's almost always a mistake; warn loudly.
        warning = (
            "\n\nNote: silent=true with approval_mode='' and an interval schedule "
            "will still prompt for approval on each firing. If you want "
            "unattended polling, set approval_mode='auto'."
        )

    return ToolResult.ok(
        f"created cron {stored.id!r} ({_format_schedule(schedule)}, "
        f"{'silent' if silent else 'announce'}, "
        f"{'auto-approve' if approval_mode == 'auto' else 'per-tool approval'})" + warning
    )


async def _tool_cron_list(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    svc = _get_cron_service(ctx)
    if svc is None:
        return ToolResult.error("cron service not available on this gateway")

    include_disabled = bool(args.get("include_disabled", True))
    # Pick up any external edits before listing so the output
    # reflects what's actually on disk.
    svc.reload_if_changed()
    jobs = svc.list(include_disabled=include_disabled)
    if not jobs:
        return ToolResult.ok("(no crons scheduled)")
    lines = [_format_job(j) for j in jobs]
    return ToolResult.ok("\n".join(lines))


async def _tool_cron_update(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    svc = _get_cron_service(ctx)
    if svc is None:
        return ToolResult.error("cron service not available on this gateway")

    job_id = args.get("id")
    if not isinstance(job_id, str) or not job_id:
        return ToolResult.error("'id' is required")

    kwargs: dict[str, Any] = {}
    if "name" in args and args["name"] is not None:
        kwargs["name"] = str(args["name"])
    if "message" in args and args["message"] is not None:
        kwargs["message"] = str(args["message"])
    if "silent" in args and args["silent"] is not None:
        kwargs["silent"] = bool(args["silent"])
    if "approval_mode" in args and args["approval_mode"] is not None:
        mode = args["approval_mode"]
        if mode not in ("", "auto"):
            return ToolResult.error("approval_mode must be '' or 'auto'")
        kwargs["approval_mode"] = mode
    if "agent_alias" in args and args["agent_alias"] is not None:
        kwargs["agent_alias"] = str(args["agent_alias"])
    if "schedule_spec" in args and args["schedule_spec"] is not None:
        spec = str(args["schedule_spec"])
        tz = str(args.get("timezone") or "UTC")
        try:
            kwargs["schedule"] = parse_schedule_spec(spec, tz=tz)
        except CronError as e:
            return ToolResult.error(f"invalid schedule: {e}")

    if not kwargs:
        return ToolResult.error("nothing to update — no fields supplied")

    try:
        updated = svc.update(job_id, **kwargs)
    except UnknownCron as e:
        return ToolResult.error(str(e))
    except CronError as e:
        return ToolResult.error(str(e))
    return ToolResult.ok(f"updated cron {updated.id!r}")


async def _tool_cron_remove(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    svc = _get_cron_service(ctx)
    if svc is None:
        return ToolResult.error("cron service not available on this gateway")
    job_id = args.get("id")
    if not isinstance(job_id, str) or not job_id:
        return ToolResult.error("'id' is required")
    if svc.remove(job_id):
        return ToolResult.ok(f"removed cron {job_id!r}")
    return ToolResult.error(f"no cron with id {job_id!r}")


async def _tool_cron_pause(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return await _set_enabled(args, ctx, enabled=False, verb="paused")


async def _tool_cron_resume(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return await _set_enabled(args, ctx, enabled=True, verb="resumed")


async def _set_enabled(
    args: dict[str, Any], ctx: ToolContext, *, enabled: bool, verb: str
) -> ToolResult:
    svc = _get_cron_service(ctx)
    if svc is None:
        return ToolResult.error("cron service not available on this gateway")
    job_id = args.get("id")
    if not isinstance(job_id, str) or not job_id:
        return ToolResult.error("'id' is required")
    try:
        svc.set_enabled(job_id, enabled)
    except UnknownCron as e:
        return ToolResult.error(str(e))
    return ToolResult.ok(f"{verb} cron {job_id!r}")


# --------------------------------------------------------------- builder


def build_cron_tools() -> list[Tool]:
    """Return the cron inline tools. Register via
    :meth:`ToolRegistry.register` after constructing the
    registry — ordering doesn't matter (no cross-tool deps)."""
    return [
        Tool(
            name="cron_add",
            description=(
                "Schedule an agent session to fire on its own. "
                "Accepts interval ('every 60s'), one-shot "
                "('at 2026-05-06T09:00:00'), or cron-expression "
                "('cron 0 9 * * *') schedules. Defaults to ask for "
                "approval; the cron's `silent` and `approval_mode` "
                "flags control whether firings announce and whether "
                "their internal tools auto-approve."
            ),
            schema=_SCHEMA_CRON_ADD,
            callable=_tool_cron_add,
            default_bucket=ApprovalBucket.ASK,
        ),
        Tool(
            name="cron_list",
            description="List scheduled crons with their next-run and status.",
            schema=_SCHEMA_CRON_LIST,
            callable=_tool_cron_list,
            default_bucket=ApprovalBucket.AUTO,
        ),
        Tool(
            name="cron_update",
            description=(
                "Modify fields on an existing cron. Only fields "
                "supplied are changed; omit what you don't want "
                "to touch."
            ),
            schema=_SCHEMA_CRON_UPDATE,
            callable=_tool_cron_update,
            default_bucket=ApprovalBucket.ASK,
        ),
        Tool(
            name="cron_remove",
            description="Delete a cron permanently.",
            schema=_SCHEMA_CRON_ID_ONLY,
            callable=_tool_cron_remove,
            default_bucket=ApprovalBucket.ASK,
        ),
        Tool(
            name="cron_pause",
            description="Temporarily disable a cron. Use cron_resume to re-enable.",
            schema=_SCHEMA_CRON_ID_ONLY,
            callable=_tool_cron_pause,
            default_bucket=ApprovalBucket.AUTO,
        ),
        Tool(
            name="cron_resume",
            description="Re-enable a previously paused cron.",
            schema=_SCHEMA_CRON_ID_ONLY,
            callable=_tool_cron_resume,
            default_bucket=ApprovalBucket.AUTO,
        ),
    ]

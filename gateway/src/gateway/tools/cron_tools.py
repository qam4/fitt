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

# NOTE: two cron fields are deliberately absent from the model-facing
# schemas — ``extra_tools`` and ``approval_mode``. Both grant a scheduled
# job authority, and a model that can grant itself authority isn't
# constrained by it.
#
# ``extra_tools`` widens what a firing can reach past
# ``cron_runner.FIRING_DEFAULT_TOOLS``. Observed 2026-08-20, within hours
# of shipping the field: gemma4 sent ``extra_tools: ["send_message"]``
# unprompted on a plain reminder. Harmless in itself (send_message is
# already a default) but it proved the model populates the field, so a
# model wanting ``project_shell`` need only ask.
#
# ``approval_mode: "auto"`` collapses ASK to AUTO for every tool a firing
# calls. Removing it costs the model nothing: with the surface reduced,
# every firing-default tool is already in the AUTO bucket except
# ``todo_remove`` (checked against the live registry 2026-08-20), so a
# model-created cron has nothing to auto-approve. What it *would* have
# bought is the compound case — a cron the model marked auto-approve, an
# operator later grants ``project_shell``, and the shell then runs
# unattended with no prompt, each party unaware of the other's half.
#
# Both are operator-only, via ``fitt cron add --grant-tool`` /
# ``--auto-approve``, where one person sees both halves at once.
#
# Tool schemas are *advertised, not enforced* — nothing in FITT validates
# model arguments against them — so deleting a property is not sufficient
# on its own. :func:`_operator_only_note` handles a model that sends one
# anyway.

_SCHEMA_CRON_ADD: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "REQUIRED. The prompt submitted to a fresh agent "
                "session when this cron fires. Should read like a "
                "self-contained user turn (e.g. 'Remind me to take "
                "out the trash.' or 'List my open PRs and "
                "summarise.')."
            ),
        },
        "schedule_spec": _SCHEDULE_SPEC_ARG,
        "name": {
            "type": "string",
            "description": (
                "Optional short label (e.g. 'morning briefing'). "
                "If omitted, it is derived from the text — you "
                "do NOT need to supply one."
            ),
        },
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
    "required": ["text", "schedule_spec"],
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
        "text": {"type": "string"},
        "schedule_spec": _SCHEDULE_SPEC_ARG,
        "silent": {"type": "boolean"},
        "agent_alias": {"type": "string"},
        "timezone": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}


# --------------------------------------------------------------- helpers


def _derive_cron_name(message: str) -> str:
    """Derive a short label from the cron's message when the
    caller didn't supply one. First line, collapsed whitespace,
    capped at 50 chars. Names need not be unique (the cron id is
    the key), so a derived label is always safe."""
    first_line = message.strip().splitlines()[0] if message.strip() else "cron"
    label = " ".join(first_line.split())[:50].strip()
    return label or "cron"


def _requested_names(raw: Any) -> list[str]:
    """Tool names a model tried to grant, in whatever shape it sent them.

    Tolerant on purpose — this is only used to *report* an ignored request,
    so a weird shape shouldn't turn into an error about a field the model
    was never offered."""
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [item.strip() for item in raw if isinstance(item, str) and item.strip()]
    return []


def _operator_only_note(args: dict[str, Any]) -> str:
    """Tell the model which authority-bearing args were dropped, and who
    can set them.

    A model can send ``extra_tools`` / ``approval_mode`` even though
    neither is in the schema, because schemas are advertised and never
    validated. They're dropped rather than honoured (see the note above
    the schemas), but dropped *out loud*: a silently ignored argument is
    how a scheduled job ends up behaving unexpectedly for a reason nobody
    can trace back to the call that caused it.

    Says nothing when the request wouldn't have changed anything — a grant
    for a tool already in the firing defaults, or ``approval_mode: ""``.
    Warning about a no-op would make a working reminder look troubled."""
    notes: list[str] = []

    requested = _requested_names(args.get("extra_tools"))
    if requested:
        # Imported here: cron_runner imports the tool registry, and this
        # module is imported while that registry is being built.
        from ..cron_runner import FIRING_DEFAULT_TOOLS

        withheld = sorted({n for n in requested if n not in FIRING_DEFAULT_TOOLS})
        if withheld:
            notes.append(
                f"this cron's firings will NOT be able to use {', '.join(withheld)} "
                "— scheduled jobs run unattended on a reduced tool surface and "
                "cannot widen it themselves. The user can allow it with "
                f"`fitt cron add --grant-tool {withheld[0]}`."
            )

    if args.get("approval_mode"):
        notes.append(
            "approval_mode was ignored — a scheduled job cannot mark itself "
            "auto-approving. It doesn't need to: everything a firing can "
            "reach by default already runs without a prompt. The user can set "
            "it with `fitt cron add --auto-approve`."
        )

    if not notes:
        return ""
    body = " ".join(f"({i + 1}) {n}" for i, n in enumerate(notes)) if len(notes) > 1 else notes[0]
    return f"\n\nNote: {body} Tell the user."


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
    if job.extra_tools:
        # Shown because "what can this unattended job reach" is the
        # question you have when auditing a cron list, and it is
        # otherwise invisible without opening cron.json.
        bits.append("grants=" + ",".join(sorted(job.extra_tools)))
    label = " ".join(bits)
    return f"- {label}  {job.name!r}"


# --------------------------------------------------------------- implementations


async def _tool_cron_add(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    svc = _get_cron_service(ctx)
    if svc is None:
        return ToolResult.error("cron service not available on this gateway")

    name = args.get("name")
    text = args.get("text")
    schedule_spec = args.get("schedule_spec")
    # Only text + schedule_spec are required. ``name`` is an
    # optional label — if the model didn't supply one, derive it
    # from the text. Requiring a third field (and one named
    # ``name``, which collides with the function's own name)
    # made small models thrash: they'd give two of the three and
    # oscillate. Observed 2026-06-08 — hermes3:8b could not set a
    # plain reminder because of it. See docs/observed-issues.md.
    # The tool arg is ``text`` (aligned with send_message /
    # learn_add); it maps to the internal CronJob.message field.
    if not isinstance(text, str) or not text.strip():
        return ToolResult.error("'text' is required and must be non-empty")
    if not isinstance(schedule_spec, str) or not schedule_spec.strip():
        return ToolResult.error("'schedule_spec' is required")
    if not isinstance(name, str) or not name.strip():
        name = _derive_cron_name(text)

    tz = str(args.get("timezone") or "UTC")
    try:
        schedule = parse_schedule_spec(schedule_spec, tz=tz)
    except CronError as e:
        return ToolResult.error(f"invalid schedule: {e}")

    silent = bool(args.get("silent", False))
    agent_alias = str(args.get("agent_alias") or "")

    job = CronJob(
        id="",
        name=name.strip(),
        message=text.strip(),
        schedule=schedule,
        silent=silent,
        # approval_mode and extra_tools are deliberately NOT taken from
        # args — both are operator-only (see the note above the schemas).
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

    # The old "silent + not auto-approved will prompt on every firing"
    # warning is gone: it was true of the full tool surface, and false now
    # that a firing only reaches tools that are already in the AUTO bucket.
    # Keeping it would have told the user to fix a problem they don't have,
    # using a field they can no longer set from chat.
    note, probe = _confirmation_note(schedule)
    return ToolResult.ok(
        f"created cron {stored.id!r} ({_format_schedule(schedule)}, "
        f"{'silent' if silent else 'announce'})"
        # Also in the payload, so a model that does mention the time gets
        # it right and the appended note is then suppressed as redundant.
        + (f"\n\n{note}" if note else "")
        + _operator_only_note(args),
        user_note=note,
        user_note_probe=probe,
    )


def _confirmation_note(sched: CronSchedule) -> tuple[str, str]:
    """The absolute local fire time, as a fact the user is guaranteed to
    see, plus the fragment that says the model already said it.

    A schedule the user can't verify is one they find out about by missing
    it. FITT had the parsing half of this bug — "remind me at 1 PM" once
    became `13:00` UTC and fired immediately — and the `[Current time]`
    preamble fixed that. Confirmation stayed broken: a live run replied
    "I've scheduled a reminder … for 15 minutes from now" with no absolute
    time, so a misparse was unverifiable until it fired.

    The first fix put the time in the tool result and *asked* the model to
    relay it. A judged run on 2026-08-20 caught that being ignored in
    roughly one sample in three ("scheduled that reminder for you in 10
    minutes"). So it moved out of the model's hands: the loop appends this
    note itself, and skips it only when the reply already contains the
    ``HH:MM`` probe. Returns ``("", "")`` for intervals and cron
    expressions, which are self-describing ("every 2h") and have no single
    instant to misread."""
    if sched.kind != "at" or sched.at_ts is None:
        return "", ""
    from datetime import datetime as _dt

    local = _dt.fromtimestamp(sched.at_ts).astimezone()
    clock = local.strftime("%H:%M")
    when = local.strftime("%a %d %b at %H:%M")
    tz_name = local.tzname() or "local time"
    return f"This fires {when} ({tz_name}).", clock


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
    if "text" in args and args["text"] is not None:
        kwargs["message"] = str(args["text"])
    if "silent" in args and args["silent"] is not None:
        kwargs["silent"] = bool(args["silent"])
    # approval_mode and extra_tools are intentionally absent here too: a
    # model that could widen an existing cron's authority has the same hole
    # as one that could set it at creation.
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
    return ToolResult.ok(f"updated cron {updated.id!r}" + _operator_only_note(args))


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
                "Schedule an agent session to fire on its own — "
                "use this for reminders WITH a time, and recurring "
                "jobs. Three-way rule: a time given -> cron_add; no "
                "time -> todo_add; wants a message right now -> "
                "send_message. Ambiguous about which? Ask. "
                "REQUIRED args: `text` and `schedule_spec` (when). "
                "`text` is the user's own request, kept as they said "
                "it — kill the pronoun swap and nothing else. 'Remind "
                "me to check my emails' stays 'Remind me to check my "
                "emails'; do NOT shorten it to the errand ('Check my "
                "emails'), because at fire time the text is replayed "
                "as a fresh user turn and the errand reads as an "
                "instruction to go and do it. "
                "Schedules: interval ('every 60s', 'every 5m', "
                "'every 2h', 'every 3d', 'every 1w'), one-shot "
                "('at 2026-05-06T09:00:00'), or cron-expression "
                "('cron 0 9 * * *'). `name` is optional (derived "
                "from the text if omitted). `silent` controls whether a "
                "firing announces its reply. "
                "Firings run unattended on a reduced tool surface: they "
                "can notify, read, and use the todo list, and nothing "
                "else. A plain reminder needs nothing more. If the job "
                "would need to run a command, write a file, or reach the "
                "network, say so in your reply and tell the user to grant "
                "it with `fitt cron add --grant-tool <tool>` — you cannot "
                "widen a scheduled job's surface yourself."
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

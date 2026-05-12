"""Cron firing → agent session bridge.

Phase 4.5 task 5. Wires the :class:`CronScheduler` to the
headless agent loop so a due cron runs through the same machinery
as a chat request — identity + memory + tools + approval + audit —
minus the HTTP surface.

Produces these events per firing:

* ``cron_fired``      — at the start, before the agent runs.
* ``cron_completed``  — on success, with the final reply as body
                         (unless ``silent=True``, in which case the
                         body is empty).
* ``cron_failed``     — on any failure (upstream, tool loop, or
                         unexpected exception). Body is the error.

``approval_mode="auto"`` is honoured by wrapping the real
approval middleware with :class:`_AutoApproveWrapper`, which
replaces ``ask`` / ``trust_session`` decisions with ``auto``.
Block stays block, deny_list stays deny_list — the override only
collapses the prompt-the-user rung. That matches the doc: "the
deny list still short-circuits."
"""

from __future__ import annotations

import logging
import time
import traceback
from typing import TYPE_CHECKING, Any

from .agent_loop import run_agent_loop
from .capabilities import build_capability_block
from .cron import CronJob
from .events import EventLog
from .events import new_entry as new_event
from .router import AliasRouter
from .tools import ApprovalDecision, Tool, ToolContext, ToolRegistry

if TYPE_CHECKING:
    from .config import Config
    from .memory import MemoryStore

_log = logging.getLogger(__name__)


class CronRunner:
    """Executes a cron firing against the agent loop.

    Stateless apart from the collaborator references it holds
    (registry, approval middleware, event log, etc). One instance
    lives on ``app.state.cron_runner``; the :class:`CronScheduler`
    uses its :meth:`fire` method as the ``on_fire`` callback.
    """

    def __init__(
        self,
        *,
        config: Config,
        tool_registry: ToolRegistry,
        approval: Any,
        memory: MemoryStore,
        events: EventLog,
        audit: Any = None,
        project_registry: Any = None,
        execution_backend: Any = None,
        capability_gaps: Any = None,
        cron_service: Any = None,
        local_shell: Any = None,
        lessons: Any = None,
        artifact_store: Any = None,
    ) -> None:
        self._config = config
        self._tool_registry = tool_registry
        self._approval = approval
        self._memory = memory
        self._events = events
        self._audit = audit
        self._project_registry = project_registry
        self._execution_backend = execution_backend
        self._capability_gaps = capability_gaps
        self._cron_service = cron_service
        self._local_shell = local_shell
        self._lessons = lessons
        self._artifact_store = artifact_store

    # -------------------------------------------------- public API

    async def fire(self, job: CronJob) -> None:
        """Run ``job`` through the agent loop and emit events.

        Called by :class:`gateway.cron_scheduler.CronScheduler`
        when a job is due. Never raises — errors become
        ``cron_failed`` events so the scheduler's
        ``last_status`` / ``last_error`` and the Telegram push
        both stay in sync.
        """
        session_key = f"cron:{job.id}:{int(time.time())}"
        alias = job.agent_alias or self._default_alias()

        # --- cron_fired ---
        self._emit(
            kind="cron_fired",
            session_key=session_key,
            title=f"cron {job.name!r}",
            body="",
            meta={"cron_id": job.id, "alias": alias},
        )

        try:
            result = await self._run(job, session_key, alias)
        except Exception as exc:  # pragma: no cover - defensive
            _log.exception("cron.run_failed", extra={"cron_id": job.id})
            self._emit(
                kind="cron_failed",
                session_key=session_key,
                title=f"cron {job.name!r} failed",
                body=f"{type(exc).__name__}: {exc}",
                meta={"cron_id": job.id, "alias": alias},
            )
            # Re-raise so the scheduler marks last_status=error.
            raise

        # Map agent-loop status to an event kind.
        if result.status == "ok":
            self._emit(
                kind="cron_completed",
                session_key=session_key,
                title=f"cron {job.name!r}",
                # Silent crons produce the event but no body —
                # Telegram push skips delivery for empty-body
                # cron_completed events (wiring lands in task 7).
                body="" if job.silent else result.assistant_text,
                meta={
                    "cron_id": job.id,
                    "alias": alias,
                    "iterations": result.iterations,
                    "silent": job.silent,
                },
            )
            # Persist the turn so the model remembers what
            # happened on this firing when the same cron fires
            # again (or when the user looks at the session
            # history manually).
            if self._memory is not None and job.message and result.assistant_text:
                try:
                    self._memory.append_turn(
                        session_key,
                        job.message,
                        result.assistant_text,
                        tool_calls=(result.tool_calls_for_memory or None),
                    )
                except Exception as exc:
                    _log.warning(
                        "cron.memory_append_failed",
                        extra={"cron_id": job.id, "error": str(exc)},
                    )
            return

        if result.status == "tool_loop_exhausted":
            self._emit(
                kind="cron_failed",
                session_key=session_key,
                title=f"cron {job.name!r} failed",
                body="tool-call loop did not terminate",
                meta={
                    "cron_id": job.id,
                    "alias": alias,
                    "iterations": result.iterations,
                    "reason": "tool_loop_exhausted",
                },
            )
            raise RuntimeError("tool_loop_exhausted")

        # upstream_error
        err_detail = (
            f"{type(result.error).__name__}: {result.error}"
            if result.error is not None
            else "unknown upstream error"
        )
        self._emit(
            kind="cron_failed",
            session_key=session_key,
            title=f"cron {job.name!r} failed",
            body=err_detail,
            meta={
                "cron_id": job.id,
                "alias": alias,
                "reason": "upstream_error",
                "traceback": _tb_tail(result.error),
            },
        )
        raise RuntimeError(err_detail)

    # -------------------------------------------------- internals

    async def _run(self, job: CronJob, session_key: str, alias: str):  # type: ignore[no-untyped-def]
        """Build the initial message list and invoke the loop.

        Kept small on purpose — cron firings mirror chat requests
        almost exactly, minus the request-body extras and stream
        wrapping. The agent loop module owns the real behaviour.
        """
        # System prompt: capability block + identity/memory + the
        # cron-runner framing.
        capability_block = build_capability_block(self._tool_registry)
        framing = _build_cron_framing(job)
        system_prefix = f"{capability_block}\n\n{framing}"
        if self._memory is not None:
            ctx = self._memory.load_context(session_key)
            if ctx.system_prefix:
                system_prefix = f"{system_prefix}\n\n{ctx.system_prefix}"
            history = ctx.history_messages
        else:
            history = []

        messages: list[dict[str, Any]] = []
        if system_prefix:
            messages.append({"role": "system", "content": system_prefix})
        messages.extend(history)
        messages.append({"role": "user", "content": job.message})

        # Observability: log a short fingerprint of the system
        # prompt so we can tell from logs whether the framing
        # actually reached the model on a given firing. Full
        # prompt would be too noisy; the first 200 chars of each
        # section header is enough to confirm "capabilities +
        # scheduled firing + identity" all present.
        _log.info(
            "cron.runner.prompt_built",
            extra={
                "cron_id": job.id,
                "system_len": len(system_prefix),
                "system_head": system_prefix[:200],
                "has_scheduled_firing_section": "[Scheduled firing]" in system_prefix,
                "has_current_time": "[Current time]" in system_prefix,
                "history_turns": len(history),
            },
        )

        # Approval wrapper honours job.approval_mode.
        approval_for_firing: Any = self._approval
        if job.approval_mode == "auto":
            approval_for_firing = _AutoApproveWrapper(self._approval)

        tool_ctx = ToolContext(
            client=job.created_by_client or "cron",
            session_key=session_key,
            projects=self._project_registry,
            backend=self._execution_backend,
            policy=self._tool_registry.policy,
            audit=self._audit,
            cron=self._cron_service,
            events=self._events,
            local_shell=self._local_shell,
            lessons=self._lessons,
        )

        alias_router = AliasRouter(self._config)

        # FITT's registered tools are injected into the request
        # body so the model can call them. Same shape the HTTP
        # endpoint uses via ``_inject_fitt_tools``.
        request_body_extras: dict[str, Any] = {
            "tools": [t.to_openai_schema() for t in self._tool_registry.list_all()],
            "tool_choice": "auto",
        }

        return await run_agent_loop(
            alias=alias,
            messages=messages,
            request_body_extras=request_body_extras,
            alias_router=alias_router,
            tool_registry=self._tool_registry,
            approval=approval_for_firing,
            tool_ctx=tool_ctx,
            session_key=session_key,
            artifact_store=self._artifact_store,
        )

    def _default_alias(self) -> str:
        """Resolve job.agent_alias='' to the gateway's default.

        Falls back to ``fitt-default`` — the user's everyday
        alias — rather than silently upgrading to ``fitt-smart``.
        The principle (roadmap §7: "models are configuration,
        not architecture") says the operator's choice wins; our
        job is to surface when that choice isn't handling a
        workload well, not to route around it invisibly.

        Tool-calling in cron firings is where weak local models
        (qwen2.5-coder:14b observed 2026-05-07) most visibly
        fail — they narrate tool JSON in content instead of
        emitting a tool_calls structure. The right response is
        (a) test whether the operator's actually-configured
        local model handles it (llm-checker toolcheck, or the
        opt-in real-model trajectory suite), and (b) let the
        operator pick a different local model or explicitly
        pin ``agent_alias: fitt-smart`` per-cron when they
        want the cloud route.

        Resolution order:

        1. ``fitt-default`` — the operator's chosen everyday
           alias. Used first because the whole alias system
           exists to let the operator pick.
        2. First alias in the map — last-resort fallback for
           test configs and unusual deployments that don't
           define fitt-default.
        """
        aliases = list(self._config.aliases.keys())
        if "fitt-default" in aliases:
            return "fitt-default"
        return aliases[0] if aliases else "fitt-default"

    def _emit(
        self,
        *,
        kind: str,
        session_key: str,
        title: str,
        body: str,
        meta: dict[str, Any],
    ) -> None:
        if self._events is None:
            return
        try:
            self._events.append(
                new_event(
                    kind=kind,
                    session_key=session_key,
                    title=title,
                    body=body,
                    meta=meta,
                )
            )
        except Exception as exc:
            _log.warning("cron.event_emit_failed", extra={"error": str(exc)})


# --------------------------------------------------------------- helpers


class _AutoApproveWrapper:
    """Wraps an ApprovalMiddleware, substituting AUTO for any
    ASK / TRUST_SESSION decisions so a cron with ``approval_mode
    == "auto"`` doesn't sit around waiting for a user it can't
    prompt.

    AUTO / BLOCK / YOLO fall through unchanged. Deny-list hits,
    which the real middleware resolves before bucket lookup,
    are preserved — we don't second-guess the deny list."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def check(
        self, tool: Tool, args: dict[str, Any], context: ToolContext
    ) -> ApprovalDecision:
        decision: ApprovalDecision = await self._inner.check(tool, args, context)
        if decision.reason in ("rejected", "denied_deny_list", "blocked"):
            # Preserve the "no" — deny list and block are
            # policy-level kill switches, not prompt-to-user
            # rungs.
            return decision
        # If the user's policy would have prompted (i.e. the
        # bucket was ASK / TRUST_SESSION before the middleware
        # decided), the inner .check returns reason="approved"
        # or reason="timeout" depending on the user response.
        # We can't see the bucket from the decision alone, so
        # the simpler rule: if the decision didn't already
        # execute, flip it to auto.
        if not decision.execute:
            return ApprovalDecision.auto(
                detail=f"cron auto-approve (would have been {decision.reason})"
            )
        return decision

    # Pass-through surface: request_approval / resolve_approval
    # don't exist in this wrapper because a cron never awaits a
    # human. The approval middleware provides them for the chat
    # handler's HTTP-facing approval endpoints.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _tb_tail(exc: BaseException | None, limit: int = 800) -> str:
    """Last ~limit chars of a traceback string. Useful meta for
    cron_failed events without blowing up the log line."""
    if exc is None:
        return ""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return tb[-limit:]


def _build_cron_framing(job: CronJob) -> str:
    """Render the cron-firing's system-prompt framing.

    Sits between the capability block and identity/memory. Its
    whole job is to stop the model from treating the stored
    ``job.message`` as a fresh instruction to *create* a cron.
    Observed live 2026-05-07: a firing of "It's time to go have
    lunch" produced a reply with JSON-in-text for a fresh
    ``cron_add`` call instead of the plain reminder. The model
    pattern-matches on "schedule-flavoured language + cron_add
    is in the tool list" and tries to re-schedule itself.

    The framing is deliberately short and concrete. Two things
    it pins:

    1. **You are the scheduled firing.** The stored message is
       what the user wanted to hear or have done at this moment,
       not a request to set up a new reminder.
    2. **Tools are allowed but not required.** A monitoring cron
       legitimately needs to call tools; a reminder cron does
       not. The framing permits both without embedding concrete
       example phrases — a naked qwen-coder happily copied a
       bracketed example ("check the build and tell me when
       it's done") as its actual input on 2026-05-07, so the
       rule now is: no example sentences in the framing prose.
       Name the tools (`send_message`, etc.), don't name a
       situation that parses as a user request.

    We include the job's name + schedule kind so the model sees
    context ("this is the 'lunch reminder' every 1d cron") and
    can phrase its reply appropriately. No stored JSON of the
    cron's internals — just the human-meaningful shape.
    """
    kind = job.schedule.kind
    # Short human-readable shape for the schedule. Mirrors the
    # cron_tools ``_format_schedule`` output but inlined here to
    # avoid an import cycle (cron_runner -> tools.cron_tools ->
    # cron_runner).
    if kind == "every" and job.schedule.every_secs is not None:
        n = job.schedule.every_secs
        if n % 3600 == 0:
            schedule_phrase = f"every {n // 3600}h"
        elif n % 60 == 0:
            schedule_phrase = f"every {n // 60}m"
        else:
            schedule_phrase = f"every {n}s"
    elif kind == "at":
        schedule_phrase = "one-shot"
    elif kind == "cron":
        schedule_phrase = f"cron {job.schedule.cron_expr}"
    else:
        schedule_phrase = kind

    return (
        "[Scheduled firing]\n"
        f"You are running as a scheduled agent session for the cron "
        f"{job.name!r} ({schedule_phrase}). The next user message in "
        "this conversation is the stored prompt the user asked you to "
        "act on at this moment. It is NOT a fresh request to create "
        "or re-create a cron — do not call `cron_add`, `cron_update`, "
        "or any other cron tool in response to it.\n"
        "\n"
        "Respond to the stored prompt the way you would respond to a "
        "fresh chat turn carrying the same text. If it asks for "
        "information, fetch it and answer. If it tells you to push a "
        "notification out, call `send_message`. If it is something "
        "the user wanted to hear out loud, deliver it as a short "
        "natural reply. Do not narrate tool JSON in your reply; "
        "actually call the tool, or don't.\n"
        "\n"
        "Match the user's preferred reply tone. Keep it brief."
    )

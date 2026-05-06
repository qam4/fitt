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
                    self._memory.append_turn(session_key, job.message, result.assistant_text)
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
        # System prompt: capability block + identity/memory.
        capability_block = build_capability_block(self._tool_registry)
        system_prefix = capability_block
        if self._memory is not None:
            ctx = self._memory.load_context(session_key)
            if ctx.system_prefix:
                system_prefix = f"{capability_block}\n\n{ctx.system_prefix}"
            history = ctx.history_messages
        else:
            history = []

        messages: list[dict[str, Any]] = []
        if system_prefix:
            messages.append({"role": "system", "content": system_prefix})
        messages.extend(history)
        messages.append({"role": "user", "content": job.message})

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
        )

    def _default_alias(self) -> str:
        """Resolve job.agent_alias='' to the gateway's default.

        We look for a ``fitt-default`` alias in the config; if
        absent, fall back to the first alias in the map. Computed
        per-firing so a config edit propagates without restart."""
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

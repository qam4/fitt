"""Judged end-to-end harness — driver pieces (Phase C).

The side-effect **snapshot** (this file, first) reads the real end state
the objective assertions check — cron jobs, the todo list, recent
events — into a plain dict that rides in the trajectory as ground
truth. The **dispatch** (turns → real chat pipeline → reply +
tool_sequence) and the ``fitt eval e2e`` CLI wiring build on top.

Kept dependency-light and app-driven so `snapshot_app` is testable
against an in-process gateway with a seeded store, no live model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import fitt_home
from .e2e_eval import DispatchFn, RunResult

# Event kinds the push pipeline deliberately does NOT deliver, mirroring
# `fitt_telegram_bot.event_pusher._SKIP_KINDS`. Approvals have their own
# UI surface, and `cron_fired` is internal bookkeeping — the user hears
# about a cron when it *finishes* (`cron_completed`), which is that
# scenario's actual delivery channel. Kept here so an objective check can
# ask "would the user have seen this?" instead of guessing at a channel;
# guessing is what made a working cron look broken.
_NOT_DELIVERED: frozenset[str] = frozenset(
    {"approval_requested", "approval_resolved", "cron_fired"}
)


def _event_dict(e: Any) -> dict[str, Any]:
    return {
        "kind": str(getattr(e, "kind", "")),
        "title": getattr(e, "title", ""),
        "body": getattr(e, "body", ""),
        "session_key": getattr(e, "session_key", ""),
    }


def snapshot_app(app: Any, *, session_id: str = "main", event_tail: int = 20) -> dict[str, Any]:
    """Read the relevant stores at run end into a JSON-able dict.

    This is the ground truth an ``OutcomeAssert`` inspects: did a cron
    get created for the right time, did the todo land, what fired. Never
    raises — a missing/failed store just yields an empty slice."""
    snap: dict[str, Any] = {}

    # Cron store — the reminder scenario asserts on this.
    cron = getattr(app.state, "cron", None)
    if cron is not None:
        try:
            cron.reload_if_changed()
            snap["cron_jobs"] = [
                {
                    "id": j.id,
                    "name": j.name,
                    "message": j.message,
                    "schedule_kind": j.schedule.kind,
                    "at_ts": j.schedule.at_ts,
                    "enabled": j.enabled,
                }
                for j in cron.list(include_disabled=True)
            ]
        except Exception as exc:  # pragma: no cover - defensive
            snap["cron_error"] = str(exc)

    # Todo list (Phase E) — read the markdown if present.
    todos_path = fitt_home() / "todos.md"
    try:
        snap["todos_text"] = todos_path.read_text(encoding="utf-8") if todos_path.exists() else ""
    except OSError:
        snap["todos_text"] = ""

    # Recent event kinds — coarse "what happened" signal — plus the
    # bodies of any agent_message events. `send_message` records delivery
    # by appending an agent_message to the event log (the poller is a
    # separate subscriber), so the event log *is* the delivery record: an
    # assertion on "did FITT actually tell me?" reads this rather than
    # mocking Telegram.
    events = getattr(app.state, "events", None)
    if events is not None:
        try:
            recent = events.read(limit=event_tail)
            snap["event_kinds"] = [getattr(e, "kind", None) for e in recent]
            snap["agent_messages"] = [
                _event_dict(e) for e in recent if getattr(e, "kind", None) == "agent_message"
            ]
            snap["deliveries"] = [
                _event_dict(e) for e in recent if str(getattr(e, "kind", "")) not in _NOT_DELIVERED
            ]
        except Exception:  # pragma: no cover - defensive
            snap["event_kinds"] = []
            snap["agent_messages"] = []
            snap["deliveries"] = []

    # The global [Learned corrections] block. Not a scenario target —
    # it's here because lessons are injected into EVERY system prompt
    # regardless of session, which makes them a cross-scenario channel:
    # a learn_add in one scenario can hand a later scenario the answer
    # it was supposed to retrieve. An assertion that cares whether a
    # fact was *retrieved* has to be able to see this.
    lessons = getattr(app.state, "lessons", None)
    if lessons is not None:
        try:
            snap["lessons_text"] = lessons.render_block()
        except Exception:  # pragma: no cover - defensive
            snap["lessons_text"] = ""

    # Every tool call the run made, from the audit log — including ones
    # made by sessions FITT started itself.
    #
    # The tool_calls on a RunResult come from the TurnLog for the session
    # the *harness* dispatched, so they cannot see a cron firing: that runs
    # in its own session, started by the scheduler. When a reminder fired
    # and the model went off and ran `project_shell` (reported from live
    # use 2026-08-17), no assertion in the harness could have noticed. The
    # audit log is the one record of every tool call regardless of who
    # started the session, which is exactly what's needed here.
    audit = getattr(app.state, "audit", None)
    if audit is not None:
        try:
            entries = audit.iter_entries()
            snap["audit_tools"] = [str(e.get("tool", "")) for e in entries]
            snap["audit_calls"] = [
                {
                    "tool": str(e.get("tool", "")),
                    "session": str(e.get("session_key", "")),
                    "decision": str(e.get("decision", "")),
                    "ok": e.get("ok"),
                }
                for e in entries
            ]
        except Exception:  # pragma: no cover - defensive
            snap["audit_tools"] = []
            snap["audit_calls"] = []

    # The plan the orchestrator's planner pass elected, if any. Flat-loop
    # turns leave this empty, which is the honest reading: no plan was
    # elected because no planner ran. `[]` and "elected not to plan" are
    # deliberately NOT distinguished here — the scenario that cares gates
    # itself on the planning feature, so it only ever sees planned runs.
    plan_store = getattr(app.state, "plan_store", None)
    if plan_store is not None:
        try:
            plan = plan_store.get(session_id)
            snap["plan_items"] = [i.to_dict() for i in plan.items] if plan is not None else []
        except Exception:  # pragma: no cover - defensive
            snap["plan_items"] = []

    return snap


def cron_at_ts_matches(
    snap: dict[str, Any], target_ts: float, *, tolerance_s: float = 900.0
) -> bool:
    """Helper for the reminder assertion: is there a one-shot (`at`) cron
    whose fire time is within ``tolerance_s`` of ``target_ts``?"""
    for job in snap.get("cron_jobs", []):
        if job.get("schedule_kind") == "at" and job.get("at_ts") is not None:
            if abs(float(job["at_ts"]) - target_ts) <= tolerance_s:
                return True
    return False


def todos_contain(snap: dict[str, Any], substring: str) -> bool:
    """Helper for the todo assertion: did the todo list gain the item?"""
    return substring.lower() in str(snap.get("todos_text", "")).lower()


def _todos_path() -> Path:
    return fitt_home() / "todos.md"


# --------------------------------------------------------------- dispatch


def _extract_reply(data: dict[str, Any]) -> str:
    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content")
        return content if isinstance(content, str) else ""
    except (KeyError, IndexError, TypeError):
        return ""


def _tool_calls_from_turns(app: Any, session_id: str) -> tuple[dict[str, Any], ...]:
    """Recover every tool the loop actually executed this run, with its
    args and result summary, in call order.

    Joins the TurnLog's ``tool_call_planned`` (carries ``args``) and
    ``tool_call_executed`` (carries ``ok`` + ``result_summary``, capped
    at 300 chars) events by ``call_id`` — the authoritative per-call
    record, complete across *all* loop iterations and both turns of a
    multi-turn scenario. (The old markdown last-timestamp parse dropped
    tools that fired in an earlier iteration — the todo case — which made
    the judge wrongly conclude "no tools were called".) Since e2e
    sessions are unique per scenario+run, every tool event for the
    session belongs to this run. Returns () when no turn log is wired."""
    turns_log = getattr(app.state, "turns", None)
    if turns_log is None:
        return ()
    try:
        planned = turns_log.read(session_id, kind="tool_call_planned")
        executed = turns_log.read(session_id, kind="tool_call_executed")
    except Exception:  # pragma: no cover - defensive
        return ()
    args_by_call = {e.meta.get("call_id"): e.meta.get("args", {}) for e in planned}
    calls: list[dict[str, Any]] = []
    for e in executed:
        calls.append(
            {
                "name": e.meta.get("tool_name", "?"),
                "args": args_by_call.get(e.meta.get("call_id"), {}),
                "ok": bool(e.meta.get("ok", True)),
                "result": e.meta.get("result_summary", ""),
            }
        )
    return tuple(calls)


_TIMELINE_KINDS = (
    "llm_request",
    "llm_call_completed",
    "tool_call_planned",
    "tool_call_executed",
    "approval_requested",
    "approval_decided",
    "turn_finished",
)


def _timeline_from_turns(app: Any, session_id: str) -> tuple[dict[str, Any], ...]:
    """Recover the per-iteration turn timeline (Tier 2).

    The full shape of what the loop did: each LLM call (tokens,
    finish_reason, how many tool calls it emitted), each planned call
    (with args), each execution (ok + result), and approvals — in order.
    This is what lets a judge diagnose *why* a turn went wrong (e.g. a
    loop that re-emits the same call every iteration) rather than only
    that it did. Returns () when no turn log is wired."""
    turns_log = getattr(app.state, "turns", None)
    if turns_log is None:
        return ()
    try:
        events = turns_log.read(session_id)
    except Exception:  # pragma: no cover - defensive
        return ()
    out: list[dict[str, Any]] = []
    for e in events:
        if e.kind not in _TIMELINE_KINDS:
            continue
        entry: dict[str, Any] = {"kind": e.kind}
        for key in (
            "iteration",
            "tool_name",
            "args",
            "ok",
            "result_summary",
            "in_tokens",
            "out_tokens",
            "finish_reason",
            "tool_calls_count",
            "decision",
            "status",
            "messages",
        ):
            if key in e.meta:
                entry[key] = e.meta[key]
        out.append(entry)
    return tuple(out)


def isolate_run_paths(cfg: Any, run_home: Path) -> Any:
    """Point every FITT_HOME-derived path at ``run_home``.

    Enumerated in one place on purpose. These paths are ``Field(
    default_factory=lambda: fitt_home() / ...)``, so they're resolved when
    the *config loads* — before the harness redirects ``FITT_HOME``. Any
    one that's forgotten silently reads or writes the operator's real
    home, and the failure looks like a model or feature defect rather than
    a leak:

    * ``index_path`` forgotten -> eval turns indexed into real memory,
      where they could surface in later recall.
    * ``skills_dir`` forgotten -> the loader scanned the real skills dir
      and found none of the fixture, so a working skills feature reported
      "the model never loaded the recipe".
    * ``logging.dir`` forgotten -> an eval run's logs, including full
      request bodies when ``server.log_bodies`` is on, appended to the
      operator's real ``~/.fitt/logs``. Found by audit 2026-08-13: this
      function promised "every FITT_HOME-derived path" and checked only
      ``cfg.memory``, so the assertion below couldn't see it. A scope
      narrower than the claim is how the first two leaked too.

    Returns the mutated config for chaining; also asserts the result, so a
    newly added path field fails loudly here rather than leaking."""
    cfg.memory = cfg.memory.model_copy(
        update={
            "identity_dir": run_home / "identity",
            "sessions_dir": run_home / "sessions",
            "skills_dir": run_home / "skills",
            "index_path": run_home / "memory" / "index.db",
        }
    )
    cfg.logging = cfg.logging.model_copy(update={"dir": run_home / "logs"})

    stray = [
        f"{section}.{name}={value}"
        for section, model in (("memory", cfg.memory), ("logging", cfg.logging))
        for name, value in vars(model).items()
        if isinstance(value, Path) and run_home not in value.parents and value != run_home
    ]
    if stray:
        raise AssertionError(
            "eval config still points outside the isolated run home: "
            + ", ".join(sorted(stray))
            + " — add it to isolate_run_paths()"
        )
    return cfg


def auto_approve_for_eval(app: Any) -> None:
    """Route every approval decision through the auto-approver.

    No human is present to tap an approval, so an ASK-bucket tool would
    block for ``approval_timeout_secs`` and then reject. The deny list is
    still enforced by the wrapper.

    Setting ``app.state.approval`` is not enough, and that's the whole
    reason this is a function. ``create_app`` passes the middleware *into*
    ``CronRunner`` at construction, so the runner holds its own reference
    and a later swap on ``app.state`` never reaches it. The ``cron_fires``
    scenario runs a real agent session through that runner: a cron whose
    ``approval_mode`` is unset (the default the model creates) would hit
    the un-wrapped middleware and hang for the 10-minute approval timeout
    before rejecting — reported as a model failure. It survives today only
    because the tool it happens to call is AUTO-bucket.

    Found by audit 2026-08-13. Any future component that captures the
    middleware at construction belongs in this function."""
    from .cron_runner import _AutoApproveWrapper

    wrapper = _AutoApproveWrapper(app.state.approval)
    app.state.approval = wrapper
    runner = getattr(app.state, "cron_runner", None)
    if runner is not None:
        runner._approval = wrapper


def ensure_session(app: Any, session_id: str) -> None:
    """Register ``session_id`` if it isn't already.

    The gateway rejects chat requests for unregistered sessions (HTTP
    400 unknown_session), so both the dispatch and the setup hooks need
    this. Idempotent. An InvalidSessionId (bad chars) is a caller bug
    and must surface, not silently become an unknown_session later."""
    registry = getattr(app.state, "session_registry", None)
    if registry is None or session_id == "main" or registry.get(session_id) is not None:
        return
    from .sessions import DuplicateSessionId

    try:
        registry.create(session_id, name=f"e2e {session_id}")
    except DuplicateSessionId:  # pragma: no cover - racy create
        pass


async def plant_turn(
    app: Any,
    *,
    session_id: str,
    user_message: str,
    assistant_message: str,
) -> None:
    """Write a completed user/assistant turn into a session's history
    and wait for it to reach the retrieval index — with no model call.

    This is the substrate for scenario setup hooks. It goes through the
    real ``MemoryStore.append_turn``, so the turn is persisted in the
    same on-disk shape a live turn produces and the indexer sees it via
    the same listener; the index stays a derivative of the markdown
    rather than something the harness fabricates.

    Why it exists: a precondition the model creates itself can change
    what the scenario measures. Asking a model to remember a fact makes
    it call ``learn_add``, whose lessons reach every later system prompt
    regardless of session — so a cross-session recall test never touches
    the retrieval index. Planting the fact directly leaves retrieval as
    the only path to it.

    Raises if memory is disabled, rather than quietly planting nothing:
    a scenario that believes its precondition landed would otherwise
    grade the model on an empty index."""
    memory = getattr(app.state, "memory", None)
    if memory is None or not getattr(memory, "enabled", False):
        raise RuntimeError(
            "cannot plant a turn: memory is disabled (set memory.enabled: true), "
            "so the fact would never be persisted or indexed"
        )

    ensure_session(app, session_id)
    memory.append_turn(session_id, user_message, assistant_message)

    # The indexer is deliberately off the hot path, so the write is
    # queued. Drain before the scenario's turns run, or the fact may not
    # be searchable yet and the model gets blamed for the race.
    indexer = getattr(app.state, "memory_indexer", None)
    if indexer is not None:
        await indexer.drain()


def build_http_dispatch(
    app: Any,
    *,
    alias: str,
    token: str,
    session_id: str = "main",
    drain_indexer: bool = True,
) -> DispatchFn:
    """Return a :data:`DispatchFn` that sends a scenario's turns through
    the *real* chat pipeline (memory injection + tool loop + persistence
    + the async indexer) over the in-process ASGI transport.

    Each turn is a separate chat request in one session, so history
    provides multi-turn continuity. Between turns the indexer is drained
    so a fact stated in an earlier turn is retrievable in a later one
    (the memory-recall scenario). ``tool_sequence`` is the tool names
    that fired on the final turn, recovered from the persisted history.

    A turn may carry a ``"session"`` key to run in a *different*
    session, suffixed onto the scenario's session id. That's what makes
    a genuine cross-session recall test possible: state a fact in one
    session, ask about it in another, where history cannot carry it and
    only ``memory_search`` can. Turns without the key use the
    scenario's own session, so existing scenarios are unaffected."""
    import httpx

    def _session_for(turn: dict[str, Any]) -> str:
        sub = turn.get("session")
        return f"{session_id}-{sub}" if sub else session_id

    ensure_session(app, session_id)

    async def _dispatch(turns: list[dict[str, Any]]) -> RunResult:
        transport = httpx.ASGITransport(app=app)
        reply = ""
        error: str | None = None
        loop_status = "ok"
        # The last turn's session is where the graded reply lands, so
        # that's the one we read tool calls and the timeline back from.
        final_session = session_id
        # Sessions touched by earlier turns, in order, so an assertion
        # can see a side effect from turn 1 that changes what turn 2 is
        # testing (a learn_add writes a global lesson).
        earlier_sessions: list[str] = []
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            for turn in turns:
                turn_session = _session_for(turn)
                ensure_session(app, turn_session)
                if turn is not turns[-1] and turn_session not in earlier_sessions:
                    earlier_sessions.append(turn_session)
                final_session = turn_session
                payload = {k: v for k, v in turn.items() if k != "session"}
                body = {"model": alias, "messages": [payload], "tool_choice": "auto"}
                client.headers["X-FITT-Session"] = turn_session
                try:
                    r = await client.post("/v1/chat/completions", json=body, timeout=300.0)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    loop_status = "dispatch_error"
                    break
                if r.status_code != 200:
                    error = f"HTTP {r.status_code}: {r.text[:200]}"
                    loop_status = "upstream_error"
                    break
                reply = _extract_reply(r.json())
                idx = getattr(app.state, "memory_indexer", None)
                if drain_indexer and idx is not None:
                    await idx.drain()
        tool_calls = _tool_calls_from_turns(app, final_session) if error is None else ()
        tool_sequence = tuple(f"{c['name']}:{'ok' if c['ok'] else 'err'}" for c in tool_calls)
        earlier: tuple[dict[str, Any], ...] = ()
        if error is None:
            for sid in earlier_sessions:
                if sid != final_session:
                    earlier = earlier + _tool_calls_from_turns(app, sid)
        return RunResult(
            reply=reply,
            tool_sequence=tool_sequence,
            tool_calls=tool_calls,
            timeline=_timeline_from_turns(app, final_session),
            loop_status=loop_status,
            error=error,
            earlier_tool_calls=earlier,
        )

    return _dispatch

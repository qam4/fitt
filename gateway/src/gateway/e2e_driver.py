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

    # Recent event kinds — coarse "what happened" signal.
    events = getattr(app.state, "events", None)
    if events is not None:
        try:
            recent = events.read(limit=event_tail)
            snap["event_kinds"] = [getattr(e, "kind", None) for e in recent]
        except Exception:  # pragma: no cover - defensive
            snap["event_kinds"] = []

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

    def _ensure_session(sid: str) -> None:
        """The gateway rejects chat requests for unregistered sessions
        (HTTP 400 unknown_session), so register before driving.
        Idempotent. An InvalidSessionId (bad chars) is a caller bug and
        must surface, not silently become an unknown_session later."""
        registry = getattr(app.state, "session_registry", None)
        if registry is None or sid == "main" or registry.get(sid) is not None:
            return
        from .sessions import DuplicateSessionId

        try:
            registry.create(sid, name=f"e2e {sid}")
        except DuplicateSessionId:  # pragma: no cover - racy create
            pass

    def _session_for(turn: dict[str, Any]) -> str:
        sub = turn.get("session")
        return f"{session_id}-{sub}" if sub else session_id

    _ensure_session(session_id)

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
                _ensure_session(turn_session)
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

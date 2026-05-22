"""Per-turn body capture (Phase 7, Slice 7.2).

Phase 4.8's :mod:`gateway.turns` already captures the
*lifecycle events* for every tool-using turn (turn_started,
llm_call_started/completed, tool_call_planned/executed,
approval_requested/decided, gap_reported, turn_finished) as
JSONL lines under
``sessions/<key>/turns/<YYYY-MM-DD>.jsonl``.

This module adds *body capture* as a sidecar JSON file per
turn:

    sessions/<key>/turns/<YYYY-MM-DD>/<turn_id>.json

The events log stays the cheap-to-tail substrate (small lines,
fast parse) that ``fitt watch``, the dashboard live view, and
the Telegram renderer consume. The sidecar holds the bulky
payloads — the dispatched message list (5K-token system
prompts), the upstream response object, the structured tool-
call chain — that you only want when you're reconstructing a
specific past turn after the fact.

Why a sidecar, not extended JSONL
---------------------------------

Decision D2 in design.md: a single big ``turn_finished`` event
with a 50KB body field would slow every tail of the events
file. The sidecar separates "what happened in order" from
"what did the model see", which is the same boundary the
operator's debugging mental model already follows.

What gets captured
------------------

Per-turn :class:`TurnCapture` carries:

* The full dispatched ``messages`` list (post memory injection,
  post tool injection — exactly what the model saw).
* The final upstream ``response`` (LiteLLM ``model_dump()``).
* Every tool call attempted, with structured args, approval
  decision, duration, result summary.
* Token counts, finish reason, fallback flag, narration warning.
* Context window from Slice 7.1's cache, plus prompt-fill
  percent computed against it.

The capture is the load-bearing piece for traceability.
"What did this turn look like" should never require reading
seven files.

Privacy posture
---------------

Capture defaults on for agent-mode clients (telegram, webui,
cli, ide-not-coding-agent) and off for ``coding-agent``.
Router-mode clients pass through the gateway with their own
system prompts that may contain user code, secrets, and
tokens; the thin-router contract Phase 4 established says
FITT shouldn't inspect or persist their bodies.

Operator override via ``traceability.default_capture`` in
config.yaml — the list of client tags that get captured.

Failure semantics
-----------------

Capture is non-blocking and never raises. An IO failure (full
disk, unwritable path, race on concurrent writes) logs a
WARNING and the turn continues. Losing traceability is worse
than losing FITT. Same posture as :mod:`gateway.turns`'s
event emission and :mod:`gateway.tool_artifacts`'s hoisting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .auth import is_router_mode_client

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- dataclasses


@dataclass(frozen=True, slots=True)
class CapturedToolCall:
    """One tool invocation captured for this turn.

    Mirrors the per-call data the agent loop already accumulates
    via :class:`gateway.tools._types.ToolResult` and
    :class:`gateway.tools._types.ApprovalDecision`. The capture
    differs in that it freezes the *outcome* — name, args,
    decision, ok/error, duration — rather than the in-flight
    state.
    """

    call_id: str
    tool_name: str
    args: dict[str, Any]
    decision: str
    """:class:`ApprovalDecision.reason` literal: ``"auto"``,
    ``"approved"``, ``"rejected"``, ``"timeout"``,
    ``"trust_session"``, ``"yolo"``, ``"blocked"``,
    ``"denied_deny_list"``."""
    decision_detail: str
    duration_ms: int
    ok: bool
    result_summary: str
    """First ~300 chars of the tool's payload. Long results land
    on disk as artifacts (see :mod:`gateway.tool_artifacts`); the
    artifact_path field points there. The summary is what fits
    cheaply in the capture."""
    artifact_path: str | None
    iteration: int


@dataclass(frozen=True, slots=True)
class TurnCapture:
    """Frozen record of one tool-using turn.

    Written once at turn-finished time as
    ``<turn_id>.json``. Read by the ``/v1/sessions/<s>/turns/<id>``
    endpoint, ``fitt turn show``, and the dashboard turns view.

    All fields are JSON-serialisable through :func:`asdict`.
    Decimal values get converted to float at the schema
    boundary in :meth:`to_dict` because Decimal isn't
    JSON-serialisable.
    """

    turn_id: str
    session_key: str
    alias: str
    """The alias the client requested (``fitt-default``). The
    actual model that served is in ``model_used``."""
    client: str
    model_used: str
    """Concrete model id (``qwen2.5-coder:14b``)."""
    backend: str
    """Backend that served (``ollama``, ``openrouter``)."""
    fallback_used: bool
    started_at: float
    finished_at: float
    dispatched_messages: list[dict[str, Any]]
    """Full message list sent to LiteLLM for the FINAL iteration
    of the tool loop. Post-memory injection, post-tool injection.
    Exactly what the model saw when it produced the final
    reply. Earlier iterations are reconstructable from the
    events log if needed."""
    response: dict[str, Any]
    """LiteLLM response ``model_dump(exclude_none=True)``."""
    tool_calls: list[CapturedToolCall]
    prompt_tokens: int
    completion_tokens: int
    context_window: int | None
    """From Slice 7.1's :class:`ContextWindowCache`. ``None`` when
    discovery failed for the bound model."""
    prompt_pct_of_window: float | None
    """``prompt_tokens / context_window * 100`` when both are
    known. ``None`` otherwise so the dashboard can render
    'unknown' rather than 0%."""
    finish_reason: str | None
    narration_warning: bool
    """Slice 7.2 D4: post-hoc flag set when the response shape
    suggests the model narrated a tool call instead of
    emitting one. Annotation only — never gates anything;
    the rolled-back live-chat signal stays rolled back."""
    iterations: int
    status: str
    """``AgentLoopResult.status``: ``"ok"``,
    ``"upstream_error"``, ``"tool_loop_exhausted"``."""

    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict. ``dataclasses.asdict`` with manual
        massaging for fields that aren't JSON-native."""
        d = asdict(self)
        # Future-proofing: if any field becomes Decimal or
        # similar, the conversion lands here. Today every field
        # is JSON-friendly already.
        return d


# --------------------------------------------------------------- store


class TurnCaptureStore:
    """Atomic, non-blocking, per-turn JSON sidecar writer.

    One instance per gateway process; lives on
    ``app.state.turn_capture``. Threading: Python's GIL plus
    POSIX rename atomicity make concurrent writes from
    different turns safe; the unique ``turn_id`` plus the
    tmp-file uuid prevent collisions.
    """

    def __init__(self, sessions_dir: Path) -> None:
        self._sessions_dir = sessions_dir

    # ------------------------------------------------ paths

    def turn_dir(self, session_key: str, day: datetime) -> Path:
        """Per-day directory for one session's captures."""
        return self._sessions_dir / session_key / "turns" / day.strftime("%Y-%m-%d")

    def turn_path(self, session_key: str, day: datetime, turn_id: str) -> Path:
        """Canonical path for a turn's capture file."""
        return self.turn_dir(session_key, day) / f"{turn_id}.json"

    # ------------------------------------------------ write

    def write(self, capture: TurnCapture) -> Path | None:
        """Write the capture to disk atomically.

        Returns the path on success, ``None`` on failure.
        Atomicity: write to ``<turn_id>.json.<rand>.tmp``, then
        rename. Half-written files don't appear under the
        canonical name. The tmp suffix is randomised so two
        concurrent writes for the same turn (shouldn't happen,
        but defensive) don't trample.

        Failures log WARNING and return ``None``. The chat
        handler ignores the return value — it's fire-and-forget.
        """
        day = datetime.fromtimestamp(capture.finished_at, tz=UTC)
        day_dir = self.turn_dir(capture.session_key, day)
        canonical = self.turn_path(capture.session_key, day, capture.turn_id)
        tmp_suffix = uuid.uuid4().hex[:8]
        tmp = canonical.with_suffix(f".json.{tmp_suffix}.tmp")

        try:
            day_dir.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                capture.to_dict(),
                ensure_ascii=False,
                indent=2,
                separators=(",", ": "),
            )
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, canonical)
            return canonical
        except (OSError, ValueError, TypeError) as exc:
            _log.warning(
                "turn_capture.write_failed",
                extra={
                    "turn_id": capture.turn_id,
                    "session_key": capture.session_key,
                    "path": str(canonical),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            # Best-effort cleanup of the tmp file. Failure here
            # is fine; tmp files in the wild get pruned by the
            # history pruner alongside the rest.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def write_async(self, capture: TurnCapture) -> asyncio.Task[Path | None]:
        """Schedule the write in a background task so the chat
        handler doesn't block on disk IO. The task's result is
        the same shape as :meth:`write`'s return value but
        callers shouldn't await it — the chat path returns its
        response before the capture lands."""
        loop = asyncio.get_running_loop()
        return loop.create_task(asyncio.to_thread(self.write, capture))

    # ------------------------------------------------ read

    def read(self, session_key: str, turn_id: str) -> TurnCapture | None:
        """Look up a captured turn by id. Searches all per-day
        directories under the session — capture timestamps may
        not match call-time exactly, so a `find` is more robust
        than computing the right day. Returns ``None`` when the
        file isn't present (turn never captured, capture failed
        silently, or the privacy default ruled it out)."""
        session_dir = self._sessions_dir / session_key / "turns"
        if not session_dir.exists():
            return None
        # Walk per-day directories. Most lookups hit recent
        # turns, so iterate newest-first.
        try:
            day_dirs = sorted(
                (p for p in session_dir.iterdir() if p.is_dir()),
                reverse=True,
            )
        except OSError:
            return None
        for day_dir in day_dirs:
            candidate = day_dir / f"{turn_id}.json"
            if candidate.exists():
                return self._load(candidate)
        return None

    def list_recent(
        self,
        session_key: str,
        *,
        limit: int = 50,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return summary records for recent captures.

        Used by the dashboard's turn list and by ``/v1/sessions/
        <s>/turns?limit=N``. Returns lightweight dicts (no
        ``dispatched_messages`` / ``response`` / ``tool_calls``)
        so listing 50 turns doesn't load megabytes of
        bodies.

        Filter: ``since`` is a UNIX timestamp; only turns whose
        ``started_at`` is at-or-after ``since`` survive. ``limit``
        is applied after sorting newest-first.
        """
        session_dir = self._sessions_dir / session_key / "turns"
        if not session_dir.exists():
            return []
        try:
            day_dirs = sorted(
                (p for p in session_dir.iterdir() if p.is_dir()),
                reverse=True,
            )
        except OSError:
            return []

        out: list[dict[str, Any]] = []
        for day_dir in day_dirs:
            try:
                files = sorted(day_dir.glob("*.json"), reverse=True)
            except OSError:
                continue
            for f in files:
                cap = self._load(f)
                if cap is None:
                    continue
                if since is not None and cap.started_at < since:
                    continue
                out.append(_summarise(cap))
                if len(out) >= limit:
                    return out
        return out

    def _load(self, path: Path) -> TurnCapture | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning(
                "turn_capture.read_failed",
                extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
            )
            return None
        try:
            tool_calls = [CapturedToolCall(**tc) for tc in data.get("tool_calls", [])]
            return TurnCapture(
                turn_id=data["turn_id"],
                session_key=data["session_key"],
                alias=data["alias"],
                client=data["client"],
                model_used=data["model_used"],
                backend=data["backend"],
                fallback_used=data["fallback_used"],
                started_at=data["started_at"],
                finished_at=data["finished_at"],
                dispatched_messages=data["dispatched_messages"],
                response=data["response"],
                tool_calls=tool_calls,
                prompt_tokens=data["prompt_tokens"],
                completion_tokens=data["completion_tokens"],
                context_window=data.get("context_window"),
                prompt_pct_of_window=data.get("prompt_pct_of_window"),
                finish_reason=data.get("finish_reason"),
                narration_warning=data.get("narration_warning", False),
                iterations=data.get("iterations", 0),
                status=data.get("status", "ok"),
                schema_version=data.get("schema_version", 1),
            )
        except (KeyError, TypeError) as exc:
            _log.warning(
                "turn_capture.shape_mismatch",
                extra={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
            )
            return None


def _summarise(cap: TurnCapture) -> dict[str, Any]:
    """Lightweight dict for the list view — drop the bodies."""
    return {
        "turn_id": cap.turn_id,
        "session_key": cap.session_key,
        "started_at": cap.started_at,
        "finished_at": cap.finished_at,
        "alias": cap.alias,
        "client": cap.client,
        "model_used": cap.model_used,
        "backend": cap.backend,
        "fallback_used": cap.fallback_used,
        "prompt_tokens": cap.prompt_tokens,
        "completion_tokens": cap.completion_tokens,
        "context_window": cap.context_window,
        "prompt_pct_of_window": cap.prompt_pct_of_window,
        "finish_reason": cap.finish_reason,
        "narration_warning": cap.narration_warning,
        "iterations": cap.iterations,
        "tool_calls_count": len(cap.tool_calls),
        "status": cap.status,
    }


# --------------------------------------------------------------- predicate


def should_capture(
    *,
    client: str,
    config_default: list[str],
    enabled: bool = True,
) -> bool:
    """Return True iff capture is enabled for this client.

    Decision D3 in design.md: agent-mode clients capture by
    default; coding-agent doesn't. Operators override via
    ``traceability.default_capture`` in config.yaml.

    The router-mode predicate (:func:`is_router_mode_client`)
    is the existing source of truth for "is this a thin-router
    client"; this function defers to it for the default but
    lets the config override raise the bar.
    """
    if not enabled:
        return False
    # Operator config wins. If the client is in the list, capture.
    if client in config_default:
        return True
    # Otherwise, agent-mode default applies. Router-mode = no
    # capture; everything else captures.
    return not is_router_mode_client(client)


# --------------------------------------------------------------- builder


@dataclass(slots=True)
class TurnCaptureBuilder:
    """Mutable accumulator the chat handler / cron runner fills
    during a turn. Built piecemeal; finalised via
    :meth:`build` at turn-finished time.

    The chat handler already has every field as a local
    variable somewhere in :mod:`gateway.chat`. This builder
    centralises the assembly so we don't proliferate
    keyword args across :func:`build_turn_capture`.
    """

    turn_id: str
    session_key: str
    alias: str
    client: str
    started_at: float
    dispatched_messages: list[dict[str, Any]] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[CapturedToolCall] = field(default_factory=list)
    model_used: str = "(unknown)"
    backend: str = "(unknown)"
    fallback_used: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    context_window: int | None = None
    finish_reason: str | None = None
    narration_warning: bool = False
    iterations: int = 0
    status: str = "ok"

    def build(self, *, finished_at: float | None = None) -> TurnCapture:
        ts = finished_at if finished_at is not None else time.time()
        pct: float | None
        if self.context_window and self.context_window > 0:
            pct = (self.prompt_tokens / self.context_window) * 100.0
        else:
            pct = None
        return TurnCapture(
            turn_id=self.turn_id,
            session_key=self.session_key,
            alias=self.alias,
            client=self.client,
            model_used=self.model_used,
            backend=self.backend,
            fallback_used=self.fallback_used,
            started_at=self.started_at,
            finished_at=ts,
            dispatched_messages=self.dispatched_messages,
            response=self.response,
            tool_calls=self.tool_calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            context_window=self.context_window,
            prompt_pct_of_window=pct,
            finish_reason=self.finish_reason,
            narration_warning=self.narration_warning,
            iterations=self.iterations,
            status=self.status,
        )

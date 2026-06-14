"""Phase 12 task 3 — record / replay of model dispatches.

Captures real model responses once, then replays them deterministically
so CI exercises the orchestration plumbing (planner -> executor ->
recovery, plan events, the whole loop) against *real output shapes*
without a live model and without non-deterministic flake. Inspired by
OpenCode's ``http-recorder`` / ``recorded-*`` fixtures.

The seam is the :class:`gateway.router.AliasRouter`: the agent loop,
planner, and orchestrator only ever call ``router.dispatch(alias, body)``
(and occasionally ``router.resolve(alias)``), so a drop-in object with
those two methods can record or replay transparently. No change to the
loop, the planner, or the orchestrator is needed — the test wires the
wrapper in where the real router would go.

Two modes, two classes:

* :class:`RecordingRouter` wraps a real ``AliasRouter``, forwards each
  dispatch to it, and appends the ``(request_key, response,
  model_used)`` interaction to an in-memory cassette. Call
  :meth:`RecordingRouter.save` to write the cassette JSON to disk. Used
  by the operator once, against a real backend.
* :class:`ReplayRouter` loads a cassette and serves dispatches from it
  with no live call. A request whose key isn't in the cassette raises
  :class:`CassetteMiss` — loud, never a silent wrong answer (Principle
  11). Used by CI / fast local tests.

Cassette shape (JSON, ``version: 1``)::

    {
      "version": 1,
      "interactions": [
        {
          "alias": "fitt-local-qwen3",
          "request_key": "<sha256 hex>",
          "request_digest": {"n_messages": 3, "tool_names": ["todowrite"]},
          "response": { ...the upstream response dict... },
          "model_used": { ...ModelConfig as JSON... },
          "fallback_used": false
        }
      ]
    }

Keying: a SHA-256 over the canonical ``(alias, body)`` with volatile
fields (``timeout``, ``stream``) stripped and object keys sorted. The
same prompt + tools therefore maps to the same key across record and
replay. When one key was recorded multiple times (e.g. the same body
dispatched twice in a turn), replay returns the recorded responses in
order — keyed *and* sequential, so identical-request turns still
replay faithfully.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_loop import response_to_dict
from .config import ModelConfig
from .router import DispatchResult

CASSETTE_VERSION = 1

# Body fields that must not contribute to the request key: they vary
# with config/runtime, not with the model's decision surface.
_VOLATILE_BODY_KEYS = frozenset({"timeout", "stream"})


class CassetteMiss(KeyError):
    """Raised on replay when no recorded interaction matches a
    dispatch. Carries the alias + a short digest so the operator can
    see *what* the loop asked for that wasn't recorded — the usual
    cause is a prompt/tool change since the cassette was captured, so
    re-record."""

    def __init__(self, alias: str, digest: dict[str, Any]) -> None:
        self.alias = alias
        self.digest = digest
        super().__init__(
            f"no recorded dispatch for alias {alias!r} matching {digest}; "
            f"the prompt or tools likely changed since the cassette was "
            f"recorded — re-record the cassette."
        )


def _canonical_body(body: dict[str, Any]) -> str:
    """Stable JSON of ``body`` with volatile keys removed and object
    keys sorted, so the same logical request always hashes the same."""
    stripped = {k: v for k, v in body.items() if k not in _VOLATILE_BODY_KEYS}
    return json.dumps(stripped, sort_keys=True, ensure_ascii=False, default=str)


def request_key(alias: str, body: dict[str, Any]) -> str:
    """Deterministic key for a ``(alias, body)`` dispatch."""
    raw = alias + "\x00" + _canonical_body(body)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request_digest(body: dict[str, Any]) -> dict[str, Any]:
    """A small, human-readable fingerprint of a request for cassette
    readability and miss diagnostics. Not used for keying."""
    messages = body.get("messages")
    n_messages = len(messages) if isinstance(messages, list) else 0
    tool_names: list[str] = []
    tools = body.get("tools")
    if isinstance(tools, list):
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    tool_names.append(fn["name"])
    return {"n_messages": n_messages, "tool_names": tool_names}


@dataclass
class _Interaction:
    alias: str
    request_key: str
    request_digest: dict[str, Any]
    response: dict[str, Any]
    model_used: dict[str, Any]
    fallback_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "request_key": self.request_key,
            "request_digest": self.request_digest,
            "response": self.response,
            "model_used": self.model_used,
            "fallback_used": self.fallback_used,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _Interaction:
        return cls(
            alias=str(d["alias"]),
            request_key=str(d["request_key"]),
            request_digest=dict(d.get("request_digest", {})),
            response=dict(d["response"]),
            model_used=dict(d["model_used"]),
            fallback_used=bool(d.get("fallback_used", False)),
        )


@dataclass
class Cassette:
    """An ordered list of recorded interactions, persisted as JSON."""

    interactions: list[_Interaction] = field(default_factory=list)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CASSETTE_VERSION,
            "interactions": [i.to_dict() for i in self.interactions],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Cassette:
        data = json.loads(path.read_text(encoding="utf-8"))
        version = data.get("version")
        if version != CASSETTE_VERSION:
            raise ValueError(
                f"cassette {path} has version {version!r}; expected {CASSETTE_VERSION}"
            )
        return cls(interactions=[_Interaction.from_dict(d) for d in data.get("interactions", [])])


class RecordingRouter:
    """Wrap a real router; forward dispatches and record them.

    Exposes the ``dispatch`` / ``resolve`` surface the loop uses, so it
    drops in where an :class:`~gateway.router.AliasRouter` would go.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.cassette = Cassette()

    def resolve(self, alias: str) -> list[ModelConfig]:
        return self._inner.resolve(alias)  # type: ignore[no-any-return]

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        result: DispatchResult = await self._inner.dispatch(alias, body)
        if result.stream is not None:
            raise NotImplementedError(
                "RecordingRouter does not record streaming dispatches; the "
                "tool loop forces non-streaming, so record those turns."
            )
        response_dict = response_to_dict(result.response) or {}
        self.cassette.interactions.append(
            _Interaction(
                alias=alias,
                request_key=request_key(alias, body),
                request_digest=_request_digest(body),
                response=response_dict,
                model_used=result.model_used.model_dump(mode="json"),
                fallback_used=result.fallback_used,
            )
        )
        return result

    def save(self, path: Path) -> None:
        self.cassette.save(path)


class ReplayRouter:
    """Serve dispatches from a cassette, with no live model.

    Keyed by ``request_key``; when a key was recorded more than once,
    the responses replay in recorded order (a per-key FIFO). A miss
    raises :class:`CassetteMiss`.
    """

    def __init__(self, cassette: Cassette) -> None:
        self._by_key: dict[str, deque[_Interaction]] = defaultdict(deque)
        for interaction in cassette.interactions:
            self._by_key[interaction.request_key].append(interaction)

    @classmethod
    def from_path(cls, path: Path) -> ReplayRouter:
        return cls(Cassette.load(path))

    def resolve(self, alias: str) -> list[ModelConfig]:
        """Return the model(s) recorded for ``alias``. Useful for code
        paths that peek at the chain; raises if the alias never
        appeared in the cassette."""
        for queue in self._by_key.values():
            for interaction in queue:
                if interaction.alias == alias:
                    return [ModelConfig.model_validate(interaction.model_used)]
        raise CassetteMiss(alias, {"reason": "alias not in cassette"})

    async def dispatch(self, alias: str, body: dict[str, Any]) -> DispatchResult:
        key = request_key(alias, body)
        queue = self._by_key.get(key)
        if not queue:
            raise CassetteMiss(alias, _request_digest(body))
        interaction = queue.popleft()
        return DispatchResult(
            response=interaction.response,
            stream=None,
            model_used=ModelConfig.model_validate(interaction.model_used),
            fallback_used=interaction.fallback_used,
        )


__all__ = [
    "CASSETTE_VERSION",
    "Cassette",
    "CassetteMiss",
    "RecordingRouter",
    "ReplayRouter",
    "request_key",
]

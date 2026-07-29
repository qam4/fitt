"""Retrieval provider contract (Phase 9 — Memory v1).

The seam that isolates the OD1 substrate fork. Cross-session recall
(semantic + keyword) is served by *one* of two interchangeable
backends — a Honcho-backed provider (the P0 spike) or a home-grown
SQLite FTS5 + embeddings provider (the same-ABC fallback) — and the
rest of Phase 9 (the async indexer, the ``memory_search`` tool, the
optional prefetch, the visibility surface) is written once against
this interface. Only the wired module changes on the spike's
outcome.

Design pointers (`.kiro/specs/phase9-memory-v1/design.md`):

* **Markdown stays ground truth.** A :class:`MemoryDoc` is derived
  from a persisted turn; the index holds nothing that isn't
  reconstructable from the markdown, so :meth:`RetrievalProvider.
  reindex` from the files reproduces retrieval behavior (Property
  1).
* **On-demand, off the hot path.** :meth:`RetrievalProvider.index`
  is called by the async indexer *after* a turn is persisted and
  the response is sent — never on the chat request path (Property
  3). Nothing here runs during dispatch.
* **Provenance travels with recall.** Every :class:`RetrievalHit`
  carries ``session_id`` + ``date`` + ``turn_anchor`` so recalled
  context is traceable to the markdown and can be labeled as
  history rather than current-turn fact (Property 5, reusing the
  Phase 5 anti-poisoning discipline).

This module is pure types + the ABC. No substrate, no I/O, no
network — importing it must stay cheap (it's on the boot path once
a provider is wired). Concrete providers land in sibling modules
(``honcho.py`` / ``local.py``) in Phase 9b.
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

# The two retrieval modes and the two scopes are closed sets — a
# Literal keeps them honest at the type level and in the tool schema
# without an Enum's ceremony.
RetrievalMode = Literal["semantic", "keyword"]
"""``semantic`` = embedding similarity ("the gist"); ``keyword`` =
exact token / phrase match, FTS-style ("the exact string"). The two
complement each other (design D5)."""

RetrievalScope = Literal["session", "all"]
"""``session`` (default) = the current session only, keeping reads
fast and isolated; ``all`` = opt-in cross-session recall, with each
hit labeled by its originating session (design D8, U3)."""


class RetrievalError(Exception):
    """Raised for detectable index misconfigurations that must fail
    loud rather than serve wrong results — chiefly a configured
    embedding model whose vector dimension disagrees with the stored
    index (Property 6, Principle 11). Callers (the async indexer,
    the tool) isolate it so chat is never affected."""


# --------------------------------------------------------------- value types


@dataclass(frozen=True, slots=True)
class MemoryDoc:
    """One indexable unit, derived from a persisted turn.

    Ground truth is the markdown; this is the derived shape the
    indexer feeds a provider. Keyed by ``(session_id,
    turn_anchor)`` so reindexing upserts rather than duplicates
    (Property 2).

    * ``session_id``: the session the turn belongs to.
    * ``date``: the turn's calendar day (the ``YYYY-MM-DD`` history
      file it came from).
    * ``turn_anchor``: a stable within-session locator for the turn
      (e.g. the turn's ISO timestamp / header id). The upsert key
      together with ``session_id``; also what ``scroll`` queries
      anchor on.
    * ``role``: ``user`` / ``assistant`` / a Phase 5 tool role. Kept
      so a provider can weight or filter by role if it wants.
    * ``text``: the turn's retrievable content (already-compact per
      Phase 5 — short tool results, hoisted large outputs).
    * ``lineage_root``: the id of the root session in a
      resume/continuation chain, so a single logical thread dedups
      to one hit in discovery results (Property 8). Falls back to
      ``session_id`` when the session has no parent.
    """

    session_id: str
    date: date
    turn_anchor: str
    role: str
    text: str
    lineage_root: str


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """A retrieval request. The three call shapes (discovery /
    scroll / browse) fall out of which fields are set (design D5),
    rather than three separate tools:

    * **discovery**: ``text`` set → ranked hits with anchored
      windows.
    * **scroll**: ``session_id`` + ``anchor`` set (no ``text``) →
      the turns neighboring an anchor.
    * **browse**: nothing but ``scope``/``k`` → most recent turns.

    ``mode`` selects semantic vs keyword for discovery; ``scope``
    defaults to the current session and must be set to ``all`` to
    reach across sessions (Property 7)."""

    text: str = ""
    mode: RetrievalMode = "semantic"
    scope: RetrievalScope = "session"
    session_id: str | None = None
    k: int = 5
    anchor: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """One retrieved excerpt with the metadata to trace it back to
    the markdown and to label its provenance in an injected block.

    * ``excerpt``: an anchored *window* around the match, not a
      whole-session dump (U1.2), so injected payloads stay bounded.
    * ``score``: provider-defined relevance (higher = better);
      comparable only within one result set.
    """

    session_id: str
    date: date
    turn_anchor: str
    excerpt: str
    score: float
    lineage_root: str


@dataclass(frozen=True, slots=True)
class IndexStats:
    """Outcome of a (re)index pass — what the offline re-index
    script reports to the operator.

    ``skipped`` counts docs already present + unchanged (the
    idempotent-upsert no-op), so a second reindex run reports
    ``indexed == 0`` and confirms Property 2."""

    indexed: int
    skipped: int
    errors: int = 0


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """Operator-facing index health (U8), and the source of the
    dimension-mismatch guard (Property 6).

    * ``embedding_model``: the model the stored index was built
      with (``None`` before the first index).
    * ``dim``: the stored vectors' dimension (``None`` for a
      keyword-only or empty index). A configured embedding model
      whose dimension disagrees with this is a detectable
      misconfiguration and must be surfaced loudly, not served as
      silent wrong-space results (Principle 11).
    * ``backend_reachable``: is the embedding / retrieval backend
      answering right now (a cheap check, no heavy probe).
    """

    doc_count: int
    last_indexed_at: datetime | None
    embedding_model: str | None
    dim: int | None
    backend_reachable: bool
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------- provider ABC


class RetrievalProvider(abc.ABC):
    """Plugin-facing contract for a cross-session recall backend.

    Both the Honcho-backed provider (P0) and the home-grown
    SQLite FTS5 + embeddings provider (fallback) implement this
    same interface, and the shared contract test suite
    (Phase 9b) runs against whichever is wired — so the fallback
    is never a second-class citizen.

    Every method is ``async``: a provider may talk to an external
    service (Honcho) or a local store, and the indexer / tool call
    it from async code. Implementations MUST NOT block the event
    loop on long synchronous I/O.

    Failure discipline mirrors the web-search provider ABC:
    :meth:`search` MUST NOT raise on an empty/cold index — it
    returns ``[]`` (Property 4). Genuine backend errors may raise,
    but the callers (indexer, tool) isolate them so chat is never
    affected (design D3)."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable identifier used in the ``memory.retrieval_backend``
        config key (e.g. ``honcho``, ``local``). Lowercase, one
        word."""

    @abc.abstractmethod
    async def index(self, doc: MemoryDoc) -> None:
        """Add or update one document in the index (upsert keyed by
        ``(session_id, turn_anchor)``).

        Called by the async indexer after turn persistence — never
        on the chat request path (design D3)."""

    @abc.abstractmethod
    async def search(self, query: RetrievalQuery) -> list[RetrievalHit]:
        """Return the hits matching ``query``, best first.

        MUST return ``[]`` (never raise) when the index is empty or
        cold (Property 4). Honors ``scope`` — without ``scope="all"``
        it MUST NOT return hits from other sessions (Property 7) —
        and dedups a resumed thread to one hit by ``lineage_root``
        (Property 8)."""

    @abc.abstractmethod
    async def reindex(self, docs: Iterable[MemoryDoc]) -> IndexStats:
        """Rebuild/refresh the index from ``docs`` (the offline
        re-index path).

        Idempotent: running twice over the same corpus yields the
        same index and results, with the second run reporting
        ``indexed == 0`` (Property 2). Callers stream the corpus by
        walking the markdown history files."""

    @abc.abstractmethod
    async def status(self) -> IndexStatus:
        """Return current index health for the visibility surface
        (U8) and the dimension-mismatch guard (Property 6). Cheap —
        no full-corpus scan, no heavy network probe."""

"""Phase 9a task 1 — the RetrievalProvider contract types.

Pure-type coverage: the value dataclasses construct and are frozen,
the ABC can't be instantiated, and a minimal concrete subclass
satisfies the interface. The behavioral contract suite (Properties
1-8, parametrized over the wired provider) lands in Phase 9b once a
real provider exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

import pytest

from gateway.retrieval import (
    IndexStats,
    IndexStatus,
    MemoryDoc,
    RetrievalHit,
    RetrievalProvider,
    RetrievalQuery,
)


def test_value_types_construct_and_are_frozen() -> None:
    doc = MemoryDoc(
        session_id="main",
        date=date(2026, 7, 2),
        turn_anchor="2026-07-02T10:00:00Z",
        role="user",
        text="remember the pid 456 monitor",
        lineage_root="main",
    )
    assert doc.session_id == "main"
    with pytest.raises(AttributeError):
        doc.text = "mutated"  # type: ignore[misc]

    hit = RetrievalHit(
        session_id="main",
        date=date(2026, 7, 2),
        turn_anchor="2026-07-02T10:00:00Z",
        excerpt="...pid 456...",
        score=0.87,
        lineage_root="main",
    )
    assert 0.0 <= hit.score <= 1.0
    with pytest.raises(AttributeError):
        hit.score = 0.1  # type: ignore[misc]


def test_query_defaults_are_current_session_semantic() -> None:
    # Default scope is the current session and mode is semantic —
    # cross-session recall must be opted into explicitly (Property 7).
    q = RetrievalQuery(text="deployment notes")
    assert q.scope == "session"
    assert q.mode == "semantic"
    assert q.k == 5
    assert q.anchor is None


def test_index_status_and_stats_shapes() -> None:
    status = IndexStatus(
        doc_count=0,
        last_indexed_at=None,
        embedding_model=None,
        dim=None,
        backend_reachable=True,
    )
    assert status.doc_count == 0
    assert status.notes == []  # default_factory list, not shared

    stats = IndexStats(indexed=0, skipped=12)
    assert stats.errors == 0  # default


def test_provider_abc_is_not_instantiable() -> None:
    with pytest.raises(TypeError):
        RetrievalProvider()  # type: ignore[abstract]


async def test_minimal_concrete_provider_satisfies_the_contract() -> None:
    """A trivial in-memory provider implements every abstract member
    and behaves per the ABC's stated failure discipline (empty index
    -> [], never raise)."""

    class _NullProvider(RetrievalProvider):
        @property
        def name(self) -> str:
            return "null"

        async def index(self, doc: MemoryDoc) -> None:
            return None

        async def search(self, query: RetrievalQuery) -> list[RetrievalHit]:
            return []  # empty/cold index -> no hits, no raise (Property 4)

        async def reindex(self, docs: Iterable[MemoryDoc]) -> IndexStats:
            return IndexStats(indexed=sum(1 for _ in docs), skipped=0)

        async def status(self) -> IndexStatus:
            return IndexStatus(
                doc_count=0,
                last_indexed_at=datetime(2026, 7, 2, 12, 0, 0),
                embedding_model=None,
                dim=None,
                backend_reachable=True,
            )

    p = _NullProvider()
    assert p.name == "null"
    assert await p.search(RetrievalQuery(text="anything")) == []
    await p.index(
        MemoryDoc(
            session_id="main",
            date=date(2026, 7, 2),
            turn_anchor="a1",
            role="user",
            text="hi",
            lineage_root="main",
        )
    )
    stats = await p.reindex([])
    assert stats.indexed == 0
    assert (await p.status()).backend_reachable is True

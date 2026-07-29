"""Phase 9b — LocalRetrievalProvider + the shared provider contract.

The behavioral half of the RetrievalProvider contract (Properties
1-2, 4-8), run through a `provider` fixture that's parametrized over
substrates — today just `local`, so adding a second provider later
means adding a fixture param, not rewriting the assertions. A
deterministic fake embedder (token-hash buckets) makes semantic
ranking reproducible without a live Ollama backend.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from gateway.retrieval import (
    LocalRetrievalProvider,
    MemoryDoc,
    RetrievalError,
    RetrievalQuery,
)


class _FakeEmbedder:
    """Deterministic bag-of-tokens embedder: each token hashes into a
    fixed bucket, so texts that share tokens land near each other in
    cosine space. No randomness, no network."""

    def __init__(self, dim: int = 16, model_id: str = "fake-embed") -> None:
        self._dim = dim
        self._model = model_id

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * self._dim
            for tok in t.lower().split():
                h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
                v[h % self._dim] += 1.0
            out.append(v)
        return out


@pytest.fixture(params=["local"])
def provider(request: Any, tmp_path: Path) -> Any:
    """Shared contract fixture. Add a substrate → add a param."""
    if request.param == "local":
        return LocalRetrievalProvider(tmp_path / "index.db", _FakeEmbedder())
    raise AssertionError(f"unknown provider param {request.param!r}")


def _doc(
    *,
    session_id: str = "main",
    anchor: str = "a1",
    text: str = "hello world",
    lineage: str | None = None,
    day: date = date(2026, 7, 1),
    role: str = "user",
) -> MemoryDoc:
    return MemoryDoc(
        session_id=session_id,
        date=day,
        turn_anchor=anchor,
        role=role,
        text=text,
        lineage_root=lineage or session_id,
    )


# --------------------------------------------------------------- Property 4


async def test_empty_index_returns_no_hits(provider: Any) -> None:
    # Cold index -> [], never raises (Property 4).
    assert await provider.search(RetrievalQuery(text="anything", scope="all")) == []


# --------------------------------------------------------------- semantic (U1)


async def test_semantic_ranks_relevant_over_unrelated(provider: Any) -> None:
    await provider.index(_doc(anchor="a1", text="deployment notes training run flakiness"))
    await provider.index(_doc(anchor="a2", text="the cat sat quietly on the warm mat"))

    hits = await provider.search(
        RetrievalQuery(text="deployment training", scope="session", session_id="main")
    )
    assert hits, "semantic search returned nothing"
    assert hits[0].turn_anchor == "a1"


# --------------------------------------------------------------- keyword (U2)


async def test_keyword_matches_exact_token(provider: Any) -> None:
    await provider.index(_doc(anchor="a1", text="the pid 456 monitor job"))
    await provider.index(_doc(anchor="a2", text="unrelated chatter about lunch"))

    hits = await provider.search(
        RetrievalQuery(text="456", mode="keyword", scope="session", session_id="main")
    )
    assert [h.turn_anchor for h in hits] == ["a1"]


# --------------------------------------------------------------- Property 5


async def test_hits_carry_provenance(provider: Any) -> None:
    await provider.index(_doc(anchor="a1", text="deployment notes", day=date(2026, 6, 15)))
    hits = await provider.search(
        RetrievalQuery(text="deployment", scope="session", session_id="main")
    )
    h = hits[0]
    assert h.session_id == "main"
    assert h.date == date(2026, 6, 15)
    assert h.turn_anchor == "a1"
    assert h.lineage_root == "main"


# --------------------------------------------------------------- Property 7


async def test_scope_session_excludes_other_sessions(provider: Any) -> None:
    await provider.index(_doc(session_id="main", anchor="a1", text="deployment in main"))
    await provider.index(_doc(session_id="other", anchor="b1", text="deployment in other"))

    scoped = await provider.search(
        RetrievalQuery(text="deployment", scope="session", session_id="main")
    )
    assert {h.session_id for h in scoped} == {"main"}

    across = await provider.search(RetrievalQuery(text="deployment", scope="all"))
    assert {h.session_id for h in across} == {"main", "other"}


# --------------------------------------------------------------- Property 8


async def test_lineage_dedup(provider: Any) -> None:
    # A resumed thread: two sessions sharing one lineage_root.
    await provider.index(
        _doc(session_id="s1", anchor="a1", text="deployment alpha", lineage="root-L")
    )
    await provider.index(
        _doc(session_id="s2", anchor="a2", text="deployment beta", lineage="root-L")
    )
    hits = await provider.search(RetrievalQuery(text="deployment", scope="all", k=5))
    assert len(hits) == 1
    assert hits[0].lineage_root == "root-L"


# --------------------------------------------------------------- browse / scroll shapes


async def test_browse_returns_recent(provider: Any) -> None:
    await provider.index(_doc(anchor="a1", text="oldest", day=date(2026, 1, 1)))
    await provider.index(_doc(anchor="a2", text="newest", day=date(2026, 7, 1)))
    hits = await provider.search(RetrievalQuery(scope="session", session_id="main", k=1))
    assert [h.turn_anchor for h in hits] == ["a2"]


async def test_scroll_returns_neighbors(provider: Any) -> None:
    for a in ("a1", "a2", "a3"):
        await provider.index(_doc(anchor=a, text=f"turn {a}"))
    hits = await provider.search(
        RetrievalQuery(scope="session", session_id="main", anchor="a2", k=1)
    )
    assert [h.turn_anchor for h in hits] == ["a1", "a2", "a3"]


# --------------------------------------------------------------- Properties 1 & 2


async def test_reindex_idempotent_and_derivable(provider: Any) -> None:
    docs = [
        _doc(anchor="a1", text="deployment notes"),
        _doc(anchor="a2", text="training run status"),
        _doc(session_id="other", anchor="b1", text="deployment elsewhere"),
    ]
    stats1 = await provider.reindex(docs)
    assert stats1.indexed == 3
    assert stats1.skipped == 0

    # Property 2: a second identical reindex changes nothing.
    stats2 = await provider.reindex(docs)
    assert stats2.indexed == 0
    assert stats2.skipped == 3

    before = await provider.search(RetrievalQuery(text="deployment", scope="all"))
    assert before

    # Property 1: the index is a derivable artifact — nuke it, rebuild
    # from the same docs, get equivalent results.
    provider._db_path.unlink()  # type: ignore[attr-defined]
    await provider.reindex(docs)
    after = await provider.search(RetrievalQuery(text="deployment", scope="all"))
    assert [h.turn_anchor for h in after] == [h.turn_anchor for h in before]


# --------------------------------------------------------------- status


async def test_status_reports_counts_and_model(provider: Any) -> None:
    empty = await provider.status()
    assert empty.doc_count == 0

    await provider.index(_doc(anchor="a1", text="hello"))
    st = await provider.status()
    assert st.doc_count == 1
    assert st.embedding_model == "fake-embed"
    assert st.dim == 16
    assert st.backend_reachable is True


# --------------------------------------------------------------- Property 6 (local-specific)


async def test_dimension_mismatch_fails_loud(tmp_path: Path) -> None:
    """A configured embedding model whose dim disagrees with the
    stored index must fail loud, not serve wrong-space results."""
    db = tmp_path / "index.db"
    p16 = LocalRetrievalProvider(db, _FakeEmbedder(dim=16))
    await p16.index(_doc(anchor="a1", text="deployment notes"))

    p8 = LocalRetrievalProvider(db, _FakeEmbedder(dim=8))
    with pytest.raises(RetrievalError):
        await p8.index(_doc(anchor="a2", text="another turn"))
    with pytest.raises(RetrievalError):
        await p8.search(RetrievalQuery(text="deployment", scope="all"))

    # status() surfaces it as a note rather than raising.
    st = await p8.status()
    assert any("dimension mismatch" in n for n in st.notes)

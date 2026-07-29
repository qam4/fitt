"""Phase 9e — prefetch block + injection ordering."""

from __future__ import annotations

from datetime import date
from typing import Any

from gateway.chat import _inject_memory
from gateway.memory import LoadedContext
from gateway.retrieval.base import RetrievalHit, RetrievalQuery
from gateway.retrieval.prefetch import prefetch_block


class _FakeProvider:
    def __init__(self, hits: list[RetrievalHit], *, raises: bool = False) -> None:
        self._hits = hits
        self._raises = raises
        self.last_query: RetrievalQuery | None = None

    async def search(self, q: RetrievalQuery) -> list[RetrievalHit]:
        if self._raises:
            raise RuntimeError("backend down")
        self.last_query = q
        return self._hits


def _hit(text: str = "we discussed the deploy pipeline") -> RetrievalHit:
    return RetrievalHit(
        session_id="main",
        date=date(2026, 6, 1),
        turn_anchor="a1",
        excerpt=text,
        score=0.8,
        lineage_root="main",
    )


async def test_prefetch_block_labels_and_scopes() -> None:
    prov = _FakeProvider([_hit()])
    block = await prefetch_block(prov, query="how did we deploy?", session_id="main", k=3)
    assert block.startswith("[Recalled context]")
    assert "[main 2026-06-01]" in block
    assert "deploy pipeline" in block
    # Prefetch scopes to the current session and samples semantically.
    assert prov.last_query is not None
    assert prov.last_query.scope == "session"
    assert prov.last_query.session_id == "main"
    assert prov.last_query.mode == "semantic"


async def test_prefetch_empty_query_returns_empty() -> None:
    prov = _FakeProvider([_hit()])
    assert await prefetch_block(prov, query="   ", session_id="main") == ""


async def test_prefetch_no_hits_returns_empty() -> None:
    assert await prefetch_block(_FakeProvider([]), query="x", session_id="main") == ""


async def test_prefetch_swallows_backend_error() -> None:
    prov = _FakeProvider([], raises=True)
    assert await prefetch_block(prov, query="x", session_id="main") == ""


async def test_prefetch_caps_excerpt_length() -> None:
    long = "word " * 200
    block = await prefetch_block(_FakeProvider([_hit(text=long)]), query="x", session_id="main")
    # The excerpt line is bounded (cap + ellipsis), not the full ~1000 chars.
    assert "..." in block
    assert len(block) < 500


def test_inject_memory_places_recalled_block_last() -> None:
    ctx = LoadedContext(system_prefix="IDENTITY_TEXT", history_messages=[])
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_memory(
        body,
        ctx,
        capability_block="CAP",
        recalled_block="[Recalled context]\n\n- excerpt",
    )
    system = out["messages"][0]
    assert system["role"] == "system"
    content = system["content"]
    # Order: capabilities, then identity, then recalled (last).
    assert (
        content.index("CAP") < content.index("IDENTITY_TEXT") < content.index("[Recalled context]")
    )

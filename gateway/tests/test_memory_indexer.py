"""Phase 9c — MemoryIndexer: turn→doc mapping + off-hot-path scheduling.

Covers the turn→doc mapping (markdown-aligned anchor + combined text),
the no-provider / no-loop no-ops, and the load-bearing Property 3
behavior at the MemoryStore boundary: append_turn returns without
waiting on (a deliberately blocked) index, so the chat path is never
gated on embedding.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from gateway.memory import MemoryStore
from gateway.retrieval import MemoryIndexer
from gateway.retrieval.base import MemoryDoc


class _RecordingProvider:
    """Duck-typed provider: MemoryIndexer only calls ``index``. An
    optional gate lets a test hold indexing open to prove it's async."""

    def __init__(self) -> None:
        self.docs: list[MemoryDoc] = []
        self.gate: asyncio.Event | None = None

    async def index(self, doc: MemoryDoc) -> None:
        if self.gate is not None:
            await self.gate.wait()
        self.docs.append(doc)


def _store(tmp_path: Path) -> MemoryStore:
    identity = tmp_path / "identity"
    identity.mkdir()
    return MemoryStore(
        identity_dir=identity,
        sessions_dir=tmp_path / "sessions",
        max_history_chars=24_000,
        enabled=True,
    )


async def test_on_turn_maps_to_markdown_aligned_doc(tmp_path: Path) -> None:
    prov = _RecordingProvider()
    idx = MemoryIndexer(prov)  # type: ignore[arg-type]
    ts = datetime(2026, 7, 2, 10, 30, 0, tzinfo=UTC)

    idx.on_turn("main", ts, "how do I deploy?", "run the compose file")
    await idx.drain()

    assert len(prov.docs) == 1
    d = prov.docs[0]
    assert d.session_id == "main"
    # Anchor equals the on-disk header stamp (isoformat with Z).
    assert d.turn_anchor == "2026-07-02T10:30:00Z"
    assert d.date == date(2026, 7, 2)
    assert d.role == "turn"
    assert "how do I deploy?" in d.text
    assert "run the compose file" in d.text
    assert d.lineage_root == "main"


async def test_provider_none_is_noop(tmp_path: Path) -> None:
    idx = MemoryIndexer(None)
    assert idx.enabled is False
    idx.on_turn("main", datetime.now(UTC), "u", "a")  # no raise
    await idx.drain()  # nothing scheduled


def test_on_turn_without_running_loop_is_safe() -> None:
    # Sync context: no running loop. on_turn must skip, not raise.
    idx = MemoryIndexer(_RecordingProvider())  # type: ignore[arg-type]
    idx.on_turn("main", datetime(2026, 7, 2, tzinfo=UTC), "u", "a")
    assert idx._tasks == set()


async def test_append_turn_does_not_block_on_indexing(tmp_path: Path) -> None:
    """Property 3 core: persistence + listener scheduling returns
    immediately even while indexing is blocked; the embed happens off
    the hot path, and completes once released."""
    store = _store(tmp_path)
    prov = _RecordingProvider()
    prov.gate = asyncio.Event()  # hold indexing open
    idx = MemoryIndexer(prov)  # type: ignore[arg-type]
    store.set_turn_listener(idx.on_turn)

    store.append_turn("main", "user text", "assistant text")
    # append_turn returned without the (blocked) index running inline.
    assert prov.docs == []
    await asyncio.sleep(0)  # let the background task start — it blocks on the gate
    assert prov.docs == []

    prov.gate.set()  # release
    await idx.drain()
    assert len(prov.docs) == 1
    assert prov.docs[0].turn_anchor.endswith("Z")


async def test_append_turn_persists_even_if_listener_raises(tmp_path: Path) -> None:
    """A listener failure must never break persistence."""
    store = _store(tmp_path)

    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("listener blew up")

    store.set_turn_listener(_boom)
    store.append_turn("main", "u", "a")  # must not raise
    # The turn is still on disk.
    text = store.history_path("main").read_text(encoding="utf-8")
    assert "u" in text and "a" in text

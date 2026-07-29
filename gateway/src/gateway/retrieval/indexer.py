"""Async memory indexer (Phase 9c).

Bridges turn persistence to the retrieval index *without* blocking the
chat path (Property 3). `MemoryStore.append_turn` calls a registered
listener after it writes a turn to disk; :meth:`MemoryIndexer.on_turn`
is that listener. It builds a :class:`MemoryDoc` and schedules a
background task to embed + index it, returning immediately — so the
embedding dispatch happens after the response is sent, never inline
with the request.

The turn→doc mapping lives here so the live path and a future
reindex-from-markdown (9f) agree: the anchor is the turn's on-disk
header stamp (:func:`turn_anchor_from_ts`) and the text is the
user+assistant blocks combined (:func:`build_turn_text`), both
reproducible from the markdown. That alignment is what makes the index
a rebuildable derivative (Property 1).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from .base import MemoryDoc

if TYPE_CHECKING:
    from .base import RetrievalProvider

_log = logging.getLogger(__name__)


def turn_anchor_from_ts(ts: datetime) -> str:
    """The turn's stable within-session locator — identical to the
    on-disk header stamp (`_format_block` in memory.py), so a
    reindex-from-markdown keys on the same anchor the live path used."""
    return ts.isoformat().replace("+00:00", "Z")


def build_turn_text(user_message: str, assistant_message: str) -> str:
    """Combine a turn's user + assistant text into one searchable doc,
    reproducibly from the two markdown blocks."""
    return f"{user_message}\n{assistant_message}".strip()


class MemoryIndexer:
    """Schedules off-hot-path indexing of persisted turns.

    Wraps an optional :class:`RetrievalProvider`. When the provider is
    ``None`` (retrieval not configured) every method is a no-op, so the
    listener can be registered unconditionally."""

    def __init__(self, provider: RetrievalProvider | None) -> None:
        self._provider = provider
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    def on_turn(
        self,
        session_id: str,
        ts: datetime,
        user_message: str,
        assistant_message: str,
        *,
        lineage_root: str | None = None,
    ) -> None:
        """Listener for ``MemoryStore.append_turn``. Schedules a
        background index task and returns immediately (Property 3).

        Safe to call with no provider (no-op) and with no running event
        loop (skips — production always calls this inside the request
        loop; a sync test calling append_turn just won't index). Never
        raises into the caller."""
        if self._provider is None:
            return
        doc = MemoryDoc(
            session_id=session_id,
            date=ts.date(),
            turn_anchor=turn_anchor_from_ts(ts),
            role="turn",
            text=build_turn_text(user_message, assistant_message),
            lineage_root=lineage_root or session_id,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("memory.index.no_running_loop; skipping background index")
            return
        task = loop.create_task(self._index_one(doc))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sync_turn(self, doc: MemoryDoc) -> None:
        """Awaitable index of one doc (tests + the reindex path)."""
        if self._provider is None:
            return
        await self._provider.index(doc)

    async def _index_one(self, doc: MemoryDoc) -> None:
        assert self._provider is not None
        try:
            await self._provider.index(doc)
        except Exception as exc:
            # Backend down / transient: chat is unaffected and the turn
            # can be re-indexed later via `fitt memory reindex` (U5.3).
            _log.warning(
                "memory.index.failed",
                extra={"session": doc.session_id, "anchor": doc.turn_anchor, "error": str(exc)},
            )

    async def drain(self) -> None:
        """Await all in-flight index tasks — for tests and graceful
        shutdown, so a just-scheduled index isn't lost."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

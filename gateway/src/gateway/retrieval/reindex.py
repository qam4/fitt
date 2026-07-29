"""Offline re-index from the markdown ground truth (Phase 9f).

Walks ``sessions/<id>/history/*.md`` and rebuilds the index from the
files. Uses the *same* turn→doc mapping as the live indexer
(:func:`turn_anchor_from_ts` + :func:`build_turn_text`), so a reindex
reproduces equivalent retrieval behavior (Property 1) and, keyed by
``(session_id, turn_anchor)``, is idempotent (Property 2).

Turns are reconstructed by grouping a day's parsed blocks by their
shared header timestamp — exactly the invariant ``append_turn`` writes
(one turn = one timestamp across its user/tool/assistant blocks).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from ..memory import _parse_turns, _Turn
from .base import IndexStats, MemoryDoc, RetrievalProvider
from .indexer import build_turn_text, turn_anchor_from_ts

_log = logging.getLogger(__name__)


def _group_by_ts(turns: list[_Turn]) -> list[tuple[datetime, list[_Turn]]]:
    """Group contiguous blocks sharing a timestamp into one turn."""
    groups: list[tuple[datetime, list[_Turn]]] = []
    for t in turns:
        if t.timestamp is None:
            continue
        if groups and groups[-1][0] == t.timestamp:
            groups[-1][1].append(t)
        else:
            groups.append((t.timestamp, [t]))
    return groups


def iter_docs_from_markdown(sessions_dir: Path) -> Iterator[MemoryDoc]:
    """Yield one :class:`MemoryDoc` per turn across every session's
    history files. Missing dir → nothing; unreadable files skipped."""
    if not sessions_dir.exists():
        return
    for session_dir in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
        history_dir = session_dir / "history"
        if not history_dir.is_dir():
            continue
        for md in sorted(history_dir.glob("*.md")):
            try:
                raw = md.read_text(encoding="utf-8")
            except OSError as e:
                _log.warning("memory.reindex.read_failed", extra={"file": str(md), "error": str(e)})
                continue
            for ts, blocks in _group_by_ts(_parse_turns(raw)):
                user = next((b.content for b in blocks if b.role == "user"), "")
                assistant = next((b.content for b in blocks if b.role == "assistant"), "")
                text = build_turn_text(user, assistant)
                if not text:
                    continue
                yield MemoryDoc(
                    session_id=session_dir.name,
                    date=ts.date(),
                    turn_anchor=turn_anchor_from_ts(ts),
                    role="turn",
                    text=text,
                    lineage_root=session_dir.name,
                )


async def reindex_from_markdown(provider: RetrievalProvider, sessions_dir: Path) -> IndexStats:
    """Rebuild the index from the markdown history. Offline + idempotent."""
    docs = list(iter_docs_from_markdown(Path(sessions_dir)))
    return await provider.reindex(docs)

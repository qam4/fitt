"""Home-grown retrieval provider (Phase 9b) — SQLite FTS5 + embeddings.

The substrate chosen over Honcho (see OD1 resolution): a
dependency-light, deployment-neutral index over FITT's own markdown.

* **Keyword search** via a SQLite FTS5 virtual table (BM25 rank).
* **Semantic search** via a stored float32 embedding per document +
  brute-force cosine in Python. Brute-force is deliberate: at
  single-user corpus scale (thousands of turns) a linear scan is
  fast, and it avoids a compiled vector extension (``sqlite-vec``),
  keeping the deployment-neutral rule clean (no native build step).
  Revisit an ANN index only if corpus size ever makes the scan bite.

Embeddings are produced by an injected :class:`Embedder` — the
provider owns *storage + retrieval*, not *how vectors are made*. At
runtime the app wires an Ollama-backed embedder resolved from
``memory.embedding_alias`` (Phase 9b task 7); tests inject a
deterministic fake. This keeps the storage/cosine logic fully
unit-testable without a live embedding backend.

SQLite work runs via ``asyncio.to_thread`` with a short-lived
connection per call — cheap at single-user scale and avoids
cross-thread connection sharing. Ground truth stays the markdown;
this DB is a rebuildable derivative (Property 1).
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
from array import array
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .base import (
    IndexStats,
    IndexStatus,
    MemoryDoc,
    RetrievalError,
    RetrievalHit,
    RetrievalProvider,
    RetrievalQuery,
)


@runtime_checkable
class Embedder(Protocol):
    """How the local provider gets vectors. Decoupled from the
    provider so the embedding model is config (an alias resolved
    elsewhere), and so tests inject a deterministic fake."""

    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT NOT NULL,
    date         TEXT NOT NULL,
    turn_anchor  TEXT NOT NULL,
    role         TEXT NOT NULL,
    text         TEXT NOT NULL,
    lineage_root TEXT NOT NULL,
    embedding    BLOB,
    dim          INTEGER,
    UNIQUE(session_id, turn_anchor)
);

CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts
    USING fts5(text, content='docs', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO docs_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression: keep alnum
    tokens, AND them together (implicit). Dropping punctuation avoids
    FTS5 syntax errors on user text and keeps matching predictable."""
    tokens = ["".join(ch for ch in tok if ch.isalnum()) for tok in text.split()]
    tokens = [t for t in tokens if t]
    return " ".join(tokens)


class LocalRetrievalProvider(RetrievalProvider):
    """SQLite FTS5 + embeddings implementation of the ABC."""

    def __init__(
        self, db_path: str | Path, embedder: Embedder, *, provider_name: str = "local"
    ) -> None:
        self._db_path = Path(db_path)
        self._embedder = embedder
        self._name = provider_name

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------- connection

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        return conn

    def _check_dim(self, conn: sqlite3.Connection) -> None:
        """Fail loud on a stored-vs-configured embedding dimension
        mismatch (Property 6, Principle 11)."""
        row = conn.execute("SELECT value FROM meta WHERE key = 'dim'").fetchone()
        if row is not None and int(row["value"]) != self._embedder.dim:
            raise RetrievalError(
                f"embedding dimension mismatch: index built at dim "
                f"{row['value']} but configured model {self._embedder.model_id!r} "
                f"is dim {self._embedder.dim}. Re-index after an embedding-model "
                f"change (`fitt memory reindex`)."
            )

    def _remember_model(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('dim', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(self._embedder.dim),),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('embedding_model', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (self._embedder.model_id,),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_indexed_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (datetime.now(UTC).isoformat(),),
        )

    # ------------------------------------------------- index

    async def index(self, doc: MemoryDoc) -> None:
        vec = (await self._embedder.embed([doc.text]))[0]
        await asyncio.to_thread(self._sync_upsert, doc, vec)

    def _sync_upsert(self, doc: MemoryDoc, vec: list[float]) -> None:
        conn = self._connect()
        try:
            self._check_dim(conn)
            conn.execute(
                "INSERT INTO docs(session_id, date, turn_anchor, role, text, "
                "lineage_root, embedding, dim) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id, turn_anchor) DO UPDATE SET "
                "date=excluded.date, role=excluded.role, text=excluded.text, "
                "lineage_root=excluded.lineage_root, embedding=excluded.embedding, "
                "dim=excluded.dim",
                (
                    doc.session_id,
                    doc.date.isoformat(),
                    doc.turn_anchor,
                    doc.role,
                    doc.text,
                    doc.lineage_root,
                    _pack(vec),
                    len(vec),
                ),
            )
            self._remember_model(conn)
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------- reindex

    async def reindex(self, docs: Iterable[MemoryDoc]) -> IndexStats:
        indexed = 0
        skipped = 0
        errors = 0
        for doc in docs:
            existing = await asyncio.to_thread(self._sync_existing_text, doc)
            if existing is not None and existing == doc.text:
                skipped += 1  # unchanged -> no re-embed (idempotency, Property 2)
                continue
            try:
                vec = (await self._embedder.embed([doc.text]))[0]
                await asyncio.to_thread(self._sync_upsert, doc, vec)
                indexed += 1
            except RetrievalError:
                raise
            except Exception:
                errors += 1
        return IndexStats(indexed=indexed, skipped=skipped, errors=errors)

    def _sync_existing_text(self, doc: MemoryDoc) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT text FROM docs WHERE session_id=? AND turn_anchor=?",
                (doc.session_id, doc.turn_anchor),
            ).fetchone()
            return None if row is None else str(row["text"])
        finally:
            conn.close()

    # ------------------------------------------------- search

    async def search(self, query: RetrievalQuery) -> list[RetrievalHit]:
        if query.anchor is not None:
            return await asyncio.to_thread(self._sync_scroll, query)
        if query.text:
            if query.mode == "keyword":
                return await asyncio.to_thread(self._sync_keyword, query)
            # semantic: embed the query, cosine over the scoped corpus.
            qvec = (await self._embedder.embed([query.text]))[0]
            return await asyncio.to_thread(self._sync_semantic, query, qvec)
        return await asyncio.to_thread(self._sync_browse, query)

    def _scope_clause(self, query: RetrievalQuery) -> tuple[str, list[str]]:
        """WHERE fragment enforcing scope. session scope requires a
        session_id and filters to it (Property 7); all scope is
        unfiltered."""
        if query.scope == "all":
            return "1=1", []
        return "session_id = ?", [query.session_id or ""]

    def _row_to_hit(self, row: sqlite3.Row, score: float) -> RetrievalHit:
        return RetrievalHit(
            session_id=str(row["session_id"]),
            date=date.fromisoformat(str(row["date"])),
            turn_anchor=str(row["turn_anchor"]),
            excerpt=str(row["text"]),
            score=score,
            lineage_root=str(row["lineage_root"]),
        )

    def _dedup_lineage(self, hits: list[RetrievalHit], k: int) -> list[RetrievalHit]:
        """Keep the best (first, already ranked) hit per lineage_root
        so a resumed thread appears once (Property 8)."""
        seen: set[str] = set()
        out: list[RetrievalHit] = []
        for h in hits:
            if h.lineage_root in seen:
                continue
            seen.add(h.lineage_root)
            out.append(h)
            if len(out) >= k:
                break
        return out

    def _sync_semantic(self, query: RetrievalQuery, qvec: list[float]) -> list[RetrievalHit]:
        conn = self._connect()
        try:
            self._check_dim(conn)
            where, params = self._scope_clause(query)
            rows = conn.execute(
                f"SELECT * FROM docs WHERE {where} AND embedding IS NOT NULL",
                params,
            ).fetchall()
            scored = [(self._cosine_row(r, qvec), r) for r in rows]
            scored.sort(key=lambda t: t[0], reverse=True)
            hits = [self._row_to_hit(r, s) for s, r in scored]
            return self._dedup_lineage(hits, query.k)
        finally:
            conn.close()

    def _cosine_row(self, row: sqlite3.Row, qvec: list[float]) -> float:
        blob = row["embedding"]
        if blob is None:
            return 0.0
        return _cosine(qvec, _unpack(blob))

    def _sync_keyword(self, query: RetrievalQuery) -> list[RetrievalHit]:
        conn = self._connect()
        try:
            match = _fts_query(query.text)
            if not match:
                return []
            where, params = self._scope_clause(query)
            rows = conn.execute(
                "SELECT d.*, bm25(docs_fts) AS rank FROM docs_fts "
                "JOIN docs d ON d.id = docs_fts.rowid "
                f"WHERE docs_fts MATCH ? AND {where} "
                "ORDER BY rank LIMIT ?",
                [match, *params, query.k * 4],
            ).fetchall()
            # bm25 is lower=better; expose a higher=better score.
            hits = [self._row_to_hit(r, -float(r["rank"])) for r in rows]
            return self._dedup_lineage(hits, query.k)
        finally:
            conn.close()

    def _sync_browse(self, query: RetrievalQuery) -> list[RetrievalHit]:
        conn = self._connect()
        try:
            where, params = self._scope_clause(query)
            rows = conn.execute(
                f"SELECT * FROM docs WHERE {where} "
                "ORDER BY date DESC, turn_anchor DESC LIMIT ?",
                [*params, query.k * 4],
            ).fetchall()
            hits = [self._row_to_hit(r, 0.0) for r in rows]
            return self._dedup_lineage(hits, query.k)
        finally:
            conn.close()

    def _sync_scroll(self, query: RetrievalQuery) -> list[RetrievalHit]:
        """Return turns neighboring ``anchor`` within a session,
        ordered by turn_anchor (the within-session locator)."""
        conn = self._connect()
        try:
            session_id = query.session_id or ""
            rows = conn.execute(
                "SELECT * FROM docs WHERE session_id=? ORDER BY turn_anchor",
                (session_id,),
            ).fetchall()
            anchors = [str(r["turn_anchor"]) for r in rows]
            if query.anchor not in anchors:
                return []
            idx = anchors.index(query.anchor)
            lo = max(0, idx - query.k)
            hi = min(len(rows), idx + query.k + 1)
            return [self._row_to_hit(r, 0.0) for r in rows[lo:hi]]
        finally:
            conn.close()

    # ------------------------------------------------- status

    async def status(self) -> IndexStatus:
        return await asyncio.to_thread(self._sync_status)

    def _sync_status(self) -> IndexStatus:
        conn = self._connect()
        try:
            doc_count = int(conn.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"])
            meta = {
                str(r["key"]): str(r["value"])
                for r in conn.execute("SELECT key, value FROM meta").fetchall()
            }
            stored_dim = int(meta["dim"]) if "dim" in meta else None
            last = meta.get("last_indexed_at")
            notes: list[str] = []
            if stored_dim is not None and stored_dim != self._embedder.dim:
                notes.append(
                    f"dimension mismatch: index dim {stored_dim} != configured "
                    f"model dim {self._embedder.dim} — reindex required"
                )
            return IndexStatus(
                doc_count=doc_count,
                last_indexed_at=datetime.fromisoformat(last) if last else None,
                embedding_model=meta.get("embedding_model"),
                dim=stored_dim,
                backend_reachable=True,
                notes=notes,
            )
        finally:
            conn.close()

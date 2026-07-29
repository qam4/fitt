"""Phase 9f — reindex-from-markdown + provider wiring."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from gateway.retrieval import LocalRetrievalProvider
from gateway.retrieval.base import RetrievalQuery
from gateway.retrieval.reindex import iter_docs_from_markdown, reindex_from_markdown
from gateway.retrieval.wiring import build_retrieval_provider

from ._fixtures import build_test_config


class _FakeEmbedder:
    def __init__(self, dim: int = 16) -> None:
        self._dim = dim

    @property
    def model_id(self) -> str:
        return "fake-embed"

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


def _seed(sessions_dir: Path, session: str, day: str, turns: list[tuple[str, str, str]]) -> None:
    """turns: list of (ts, role, content) written in the on-disk format."""
    p = sessions_dir / session / "history" / f"{day}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    blocks = [f"## {ts} {role}\n\n{content}\n\n" for ts, role, content in turns]
    p.write_text("".join(blocks), encoding="utf-8")


def test_iter_docs_reconstructs_turns(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _seed(
        sessions,
        "main",
        "2026-07-01",
        [
            ("2026-07-01T10:00:00Z", "user", "how do I deploy?"),
            ("2026-07-01T10:00:00Z", "assistant", "run compose up"),
            ("2026-07-01T11:00:00Z", "user", "small talk"),
            ("2026-07-01T11:00:00Z", "assistant", "hello"),
        ],
    )
    docs = list(iter_docs_from_markdown(sessions))
    assert len(docs) == 2
    d0 = docs[0]
    assert d0.session_id == "main"
    assert d0.turn_anchor == "2026-07-01T10:00:00Z"
    assert d0.date == date(2026, 7, 1)
    assert d0.role == "turn"
    assert "how do I deploy?" in d0.text
    assert "run compose up" in d0.text


async def test_reindex_equivalent_and_idempotent(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _seed(
        sessions,
        "main",
        "2026-07-01",
        [
            ("2026-07-01T10:00:00Z", "user", "deployment pipeline notes"),
            ("2026-07-01T10:00:00Z", "assistant", "documented the deploy steps"),
        ],
    )
    _seed(
        sessions,
        "other",
        "2026-06-01",
        [
            ("2026-06-01T09:00:00Z", "user", "deployment in the other project"),
            ("2026-06-01T09:00:00Z", "assistant", "noted"),
        ],
    )
    prov = LocalRetrievalProvider(tmp_path / "index.db", _FakeEmbedder())

    stats1 = await reindex_from_markdown(prov, sessions)
    assert stats1.indexed == 2
    before = await prov.search(RetrievalQuery(text="deployment", scope="all"))
    assert before

    # Property 2: a second reindex from the same markdown changes nothing.
    stats2 = await reindex_from_markdown(prov, sessions)
    assert stats2.indexed == 0
    assert stats2.skipped == 2

    # Property 1: nuke the index, rebuild from markdown, equivalent results.
    prov._db_path.unlink()  # type: ignore[attr-defined]
    await reindex_from_markdown(prov, sessions)
    after = await prov.search(RetrievalQuery(text="deployment", scope="all"))
    assert [h.turn_anchor for h in after] == [h.turn_anchor for h in before]


async def test_reindex_missing_sessions_dir_is_empty(tmp_path: Path) -> None:
    prov = LocalRetrievalProvider(tmp_path / "index.db", _FakeEmbedder())
    stats = await reindex_from_markdown(prov, tmp_path / "nope")
    assert stats.indexed == 0


def test_build_retrieval_provider_gated_on_alias(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    assert build_retrieval_provider(cfg) is None

    cfg.memory.embedding_alias = "fitt-default"
    prov = build_retrieval_provider(cfg)
    assert isinstance(prov, LocalRetrievalProvider)

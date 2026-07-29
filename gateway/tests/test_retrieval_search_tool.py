"""Phase 9d — the memory_search tool + conditional registration."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from gateway.projects import ProjectRegistry
from gateway.retrieval.base import RetrievalHit, RetrievalQuery
from gateway.tools import ToolContext, build_core_tool_registry
from gateway.tools.retrieval_search import build_retrieval_tools

from ._fixtures import build_test_config


class _FakeProvider:
    def __init__(self, hits: list[RetrievalHit]) -> None:
        self._hits = hits
        self.last_query: RetrievalQuery | None = None

    async def search(self, q: RetrievalQuery) -> list[RetrievalHit]:
        self.last_query = q
        return self._hits


def _ctx(tmp_path: Path, provider: Any) -> ToolContext:
    return ToolContext(
        client="cli",
        session_key="main",
        projects=ProjectRegistry(tmp_path / "projects.yaml"),
        retrieval=provider,
    )


def _hit(anchor: str = "a1", session: str = "main", text: str = "deployment notes") -> RetrievalHit:
    return RetrievalHit(
        session_id=session,
        date=date(2026, 6, 15),
        turn_anchor=anchor,
        excerpt=text,
        score=0.9,
        lineage_root=session,
    )


def test_tool_shape() -> None:
    tools = build_retrieval_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "memory_search"
    assert t.description.strip()
    assert t.default_bucket.value == "auto"
    assert t.schema["required"] == []


async def test_search_formats_hits(tmp_path: Path) -> None:
    prov = _FakeProvider([_hit(text="deployment alpha"), _hit(anchor="a2", text="deployment beta")])
    tool = build_retrieval_tools()[0]
    res = await tool.callable({"query": "deployment"}, _ctx(tmp_path, prov))
    assert not res.is_error
    assert "deployment alpha" in res.payload
    assert "deployment beta" in res.payload
    assert "[main 2026-06-15]" in res.payload
    # Defaults threaded into the query: current session, semantic.
    assert prov.last_query is not None
    assert prov.last_query.scope == "session"
    assert prov.last_query.session_id == "main"
    assert prov.last_query.mode == "semantic"


async def test_scope_all_passes_through(tmp_path: Path) -> None:
    prov = _FakeProvider([])
    tool = build_retrieval_tools()[0]
    res = await tool.callable({"query": "x", "scope": "all"}, _ctx(tmp_path, prov))
    assert not res.is_error
    assert res.payload == "no matching memory found"
    assert prov.last_query is not None
    assert prov.last_query.scope == "all"


async def test_no_provider_returns_error(tmp_path: Path) -> None:
    tool = build_retrieval_tools()[0]
    res = await tool.callable({"query": "x"}, _ctx(tmp_path, None))
    assert res.is_error
    assert "isn't configured" in res.payload


async def test_invalid_mode_rejected(tmp_path: Path) -> None:
    tool = build_retrieval_tools()[0]
    res = await tool.callable({"query": "x", "mode": "bogus"}, _ctx(tmp_path, _FakeProvider([])))
    assert res.is_error


def test_registered_only_when_embedding_alias_set(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    assert "memory_search" not in build_core_tool_registry(cfg).list_names()

    cfg.memory.embedding_alias = "fitt-default"
    assert "memory_search" in build_core_tool_registry(cfg).list_names()

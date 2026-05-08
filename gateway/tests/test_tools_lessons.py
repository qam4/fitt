"""Tests for Phase 5 — ``learn_add`` / ``learn_list`` / ``learn_remove``.

Unit-level shape: each tool with a real ``LessonsStore`` via
``ToolContext``. No HTTP, no approval middleware — those are
covered at the registry layer already.
"""

from __future__ import annotations

from pathlib import Path

from gateway.lessons import LessonsStore, default_lessons_path
from gateway.projects import ProjectRegistry
from gateway.tools._types import ApprovalBucket, ToolContext
from gateway.tools.lessons import build_lessons_tools


def _ctx(tmp_path: Path, *, with_store: bool = True) -> ToolContext:
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    lessons = LessonsStore(default_lessons_path(identity_dir)) if with_store else None
    return ToolContext(
        client="telegram",
        session_key="main",
        projects=ProjectRegistry(config_path=tmp_path / "projects.yaml"),
        lessons=lessons,
    )


# --------------------------------------------------------------- build_lessons_tools


def test_build_lessons_tools_returns_three_tools() -> None:
    tools = build_lessons_tools()
    names = [t.name for t in tools]
    assert names == ["learn_add", "learn_list", "learn_remove"]


def test_learn_list_has_auto_bucket() -> None:
    """Listing is read-only and low-risk → auto."""
    tools = {t.name: t for t in build_lessons_tools()}
    assert tools["learn_list"].default_bucket == ApprovalBucket.AUTO


def test_mutation_tools_have_ask_bucket() -> None:
    """Adding and removing persist across sessions → ask."""
    tools = {t.name: t for t in build_lessons_tools()}
    assert tools["learn_add"].default_bucket == ApprovalBucket.ASK
    assert tools["learn_remove"].default_bucket == ApprovalBucket.ASK


# --------------------------------------------------------------- learn_add


async def test_learn_add_persists_lesson(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    result = await tools["learn_add"].callable({"text": "always use uv"}, ctx)
    assert not result.is_error
    assert "always use uv" in result.payload

    # Round-trip via the store.
    lessons = ctx.lessons.read()
    assert len(lessons) == 1
    assert lessons[0].text == "always use uv"
    assert lessons[0].category is None


async def test_learn_add_accepts_category(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    await tools["learn_add"].callable({"text": "prefer ruff", "category": "tooling"}, ctx)
    lessons = ctx.lessons.read()
    assert lessons[0].category == "tooling"


async def test_learn_add_empty_text_errors(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    result = await tools["learn_add"].callable({"text": ""}, ctx)
    assert result.is_error
    result = await tools["learn_add"].callable({"text": "   "}, ctx)
    assert result.is_error


async def test_learn_add_missing_store_errors(tmp_path: Path) -> None:
    """A context without a LessonsStore should surface a clear
    error so the gateway-bug is debuggable."""
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path, with_store=False)
    result = await tools["learn_add"].callable({"text": "hi"}, ctx)
    assert result.is_error
    assert "lessons store" in result.payload.lower()


# --------------------------------------------------------------- learn_list


async def test_learn_list_empty(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    result = await tools["learn_list"].callable({}, ctx)
    assert not result.is_error
    assert "no lessons" in result.payload.lower()


async def test_learn_list_shows_bullets(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    await tools["learn_add"].callable({"text": "always use uv"}, ctx)
    await tools["learn_add"].callable({"text": "prefer ruff", "category": "tooling"}, ctx)
    result = await tools["learn_list"].callable({}, ctx)
    assert not result.is_error
    assert "- always use uv" in result.payload
    assert "- [tooling] prefer ruff" in result.payload


# --------------------------------------------------------------- learn_remove


async def test_learn_remove_substring(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    await tools["learn_add"].callable({"text": "always use uv, not pip"}, ctx)
    await tools["learn_add"].callable({"text": "UV runs on Windows"}, ctx)
    await tools["learn_add"].callable({"text": "prefer ruff"}, ctx)

    result = await tools["learn_remove"].callable({"substring": "uv"}, ctx)
    assert not result.is_error
    assert "2" in result.payload
    remaining = [lsn.text for lsn in ctx.lessons.read()]
    assert remaining == ["prefer ruff"]


async def test_learn_remove_no_match(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    await tools["learn_add"].callable({"text": "always use uv"}, ctx)
    result = await tools["learn_remove"].callable({"substring": "nonexistent"}, ctx)
    assert not result.is_error
    assert "no lessons matched" in result.payload.lower()


async def test_learn_remove_empty_substring_rejected(tmp_path: Path) -> None:
    """An empty substring would wipe every lesson. The tool
    must refuse rather than silently erase."""
    tools = {t.name: t for t in build_lessons_tools()}
    ctx = _ctx(tmp_path)
    await tools["learn_add"].callable({"text": "keep me"}, ctx)

    result = await tools["learn_remove"].callable({"substring": ""}, ctx)
    assert result.is_error
    result = await tools["learn_remove"].callable({"substring": "   "}, ctx)
    assert result.is_error

    # Lesson untouched.
    assert len(ctx.lessons.read()) == 1

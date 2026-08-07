"""Tests for Phase E — ``todo_add`` / ``todo_list`` / ``todo_done`` /
``todo_remove``.

Unit-level shape: each tool with a real ``TodoStore`` via
``ToolContext``. No HTTP, no approval middleware — those are covered at
the registry layer already.
"""

from __future__ import annotations

from pathlib import Path

from gateway.projects import ProjectRegistry
from gateway.todos import TodoStore, default_todos_path
from gateway.tools._types import ApprovalBucket, ToolContext
from gateway.tools.todo_tools import build_todo_tools


def _ctx(tmp_path: Path, *, with_store: bool = True) -> ToolContext:
    store = TodoStore(default_todos_path(tmp_path)) if with_store else None
    return ToolContext(
        client="telegram",
        session_key="main",
        projects=ProjectRegistry(config_path=tmp_path / "projects.yaml"),
        todos=store,
    )


# --------------------------------------------------------------- build


def test_build_todo_tools_returns_four_tools() -> None:
    names = [t.name for t in build_todo_tools()]
    assert names == ["todo_add", "todo_list", "todo_done", "todo_remove"]


def test_buckets() -> None:
    tools = {t.name: t for t in build_todo_tools()}
    assert tools["todo_add"].default_bucket == ApprovalBucket.AUTO
    assert tools["todo_list"].default_bucket == ApprovalBucket.AUTO
    assert tools["todo_done"].default_bucket == ApprovalBucket.AUTO
    assert tools["todo_remove"].default_bucket == ApprovalBucket.ASK


# --------------------------------------------------------------- add


async def test_todo_add_persists(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    result = await tools["todo_add"].callable({"text": "call the doctor"}, ctx)
    assert not result.is_error
    assert "call the doctor" in result.payload
    todos = ctx.todos.read()
    assert len(todos) == 1
    assert todos[0].text == "call the doctor"
    assert todos[0].done is False


async def test_todo_add_empty_text_errors(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    assert (await tools["todo_add"].callable({"text": ""}, ctx)).is_error
    assert (await tools["todo_add"].callable({"text": "   "}, ctx)).is_error


async def test_todo_add_missing_store_errors(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path, with_store=False)
    result = await tools["todo_add"].callable({"text": "hi"}, ctx)
    assert result.is_error
    assert "todo store" in result.payload.lower()


# --------------------------------------------------------------- list


async def test_todo_list_empty(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    result = await tools["todo_list"].callable({}, ctx)
    assert not result.is_error
    assert "no open todos" in result.payload.lower()


async def test_todo_list_shows_open_only_by_default(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    await tools["todo_add"].callable({"text": "call the doctor"}, ctx)
    await tools["todo_add"].callable({"text": "buy milk"}, ctx)
    await tools["todo_done"].callable({"substring": "doctor"}, ctx)

    result = await tools["todo_list"].callable({}, ctx)
    assert "- [ ] buy milk" in result.payload
    assert "doctor" not in result.payload

    with_done = await tools["todo_list"].callable({"include_done": True}, ctx)
    assert "- [x] call the doctor" in with_done.payload
    assert "- [ ] buy milk" in with_done.payload


# --------------------------------------------------------------- done


async def test_todo_done_marks_matching(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    await tools["todo_add"].callable({"text": "call the doctor"}, ctx)
    result = await tools["todo_done"].callable({"substring": "doctor"}, ctx)
    assert not result.is_error
    assert "1" in result.payload
    assert ctx.todos.read()[0].done is True


async def test_todo_done_no_match(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    await tools["todo_add"].callable({"text": "buy milk"}, ctx)
    result = await tools["todo_done"].callable({"substring": "doctor"}, ctx)
    assert not result.is_error
    assert "no open todo matched" in result.payload.lower()


async def test_todo_done_empty_substring_errors(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    assert (await tools["todo_done"].callable({"substring": ""}, ctx)).is_error


# --------------------------------------------------------------- remove


async def test_todo_remove_substring(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    await tools["todo_add"].callable({"text": "call the doctor"}, ctx)
    await tools["todo_add"].callable({"text": "buy milk"}, ctx)
    result = await tools["todo_remove"].callable({"substring": "doctor"}, ctx)
    assert not result.is_error
    assert "1" in result.payload
    assert [t.text for t in ctx.todos.read()] == ["buy milk"]


async def test_todo_remove_empty_substring_rejected(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path)
    await tools["todo_add"].callable({"text": "keep me"}, ctx)
    assert (await tools["todo_remove"].callable({"substring": ""}, ctx)).is_error
    assert (await tools["todo_remove"].callable({"substring": "  "}, ctx)).is_error
    assert len(ctx.todos.read()) == 1


async def test_todo_remove_missing_store_errors(tmp_path: Path) -> None:
    tools = {t.name: t for t in build_todo_tools()}
    ctx = _ctx(tmp_path, with_store=False)
    result = await tools["todo_remove"].callable({"substring": "x"}, ctx)
    assert result.is_error

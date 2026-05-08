"""Phase 5 — verify lessons land in the system prompt.

Tests that :meth:`MemoryStore.load_context` composes the
``[Learned corrections]`` block alongside the existing identity
content, in the right order, with correct behaviour when the
lessons store is missing or empty.
"""

from __future__ import annotations

from pathlib import Path

from gateway.lessons import LessonsStore, default_lessons_path
from gateway.memory import MemoryStore


def _make_store(
    tmp_path: Path,
    *,
    with_lessons: bool = True,
) -> tuple[MemoryStore, LessonsStore | None]:
    identity_dir = tmp_path / "identity"
    sessions_dir = tmp_path / "sessions"
    identity_dir.mkdir()
    lessons = None
    if with_lessons:
        lessons = LessonsStore(default_lessons_path(identity_dir))
    store = MemoryStore(
        identity_dir=identity_dir,
        sessions_dir=sessions_dir,
        max_history_chars=24_000,
        enabled=True,
        lessons=lessons,
    )
    return store, lessons


def test_load_context_no_lessons_no_block(tmp_path: Path) -> None:
    """When the store has no lessons, the system_prefix is
    identity-only — no empty ``[Learned corrections]`` header."""
    store, _ = _make_store(tmp_path)
    ctx = store.load_context("main")
    assert "[Learned corrections]" not in ctx.system_prefix
    # Default identity template mentions "About Me" etc.
    assert "FITT" in ctx.system_prefix or "About Me" in ctx.system_prefix


def test_load_context_injects_lessons_block(tmp_path: Path) -> None:
    """When lessons exist, the block renders after identity."""
    store, lessons = _make_store(tmp_path)
    assert lessons is not None
    lessons.add("always use uv, not pip")
    lessons.add("prefer ruff format", category="tooling")

    ctx = store.load_context("main")
    assert "[Learned corrections]" in ctx.system_prefix
    assert "- always use uv, not pip" in ctx.system_prefix
    assert "- [tooling] prefer ruff format" in ctx.system_prefix


def test_load_context_lessons_come_after_identity(tmp_path: Path) -> None:
    """Order: identity first (who am I), lessons after (what
    corrections I've learned). A model reading top-down should
    see stable voice before terse recent overrides."""
    store, lessons = _make_store(tmp_path)
    assert lessons is not None
    lessons.add("always use uv")

    ctx = store.load_context("main")
    # "About Me" appears in the user.md default template.
    user_idx = ctx.system_prefix.find("About Me")
    lessons_idx = ctx.system_prefix.find("[Learned corrections]")
    assert user_idx >= 0
    assert lessons_idx >= 0
    assert user_idx < lessons_idx


def test_load_context_without_lessons_store(tmp_path: Path) -> None:
    """A MemoryStore constructed without a LessonsStore still
    works (back-compat; older tests don't provide one)."""
    store, _ = _make_store(tmp_path, with_lessons=False)
    ctx = store.load_context("main")
    assert "[Learned corrections]" not in ctx.system_prefix
    # Identity still loads.
    assert ctx.system_prefix != ""


def test_load_context_disabled_memory_returns_empty(tmp_path: Path) -> None:
    """When memory is disabled entirely, lessons are also
    skipped — disabling memory means "no injection of anything",
    which is what the flag promises."""
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    lessons = LessonsStore(default_lessons_path(identity_dir))
    lessons.add("would-be ignored")

    store = MemoryStore(
        identity_dir=identity_dir,
        sessions_dir=tmp_path / "sessions",
        max_history_chars=24_000,
        enabled=False,
        lessons=lessons,
    )
    ctx = store.load_context("main")
    assert ctx.system_prefix == ""
    assert ctx.history_messages == []


def test_lessons_edit_picked_up_on_next_load(tmp_path: Path) -> None:
    """An external edit to lessons.md (or a fresh `learn_add`)
    is reflected on the next ``load_context`` call without a
    restart."""
    store, lessons = _make_store(tmp_path)
    assert lessons is not None

    first = store.load_context("main")
    assert "[Learned corrections]" not in first.system_prefix

    lessons.add("fresh lesson")

    second = store.load_context("main")
    assert "[Learned corrections]" in second.system_prefix
    assert "fresh lesson" in second.system_prefix

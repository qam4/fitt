"""Tests for Phase 5 — LessonsStore.

Unit-level coverage of the four public behaviours:

* add / list / remove round-trip through the file.
* Max-entries ceiling drops oldest when exceeded.
* mtime-based freshness: an external edit is picked up on
  the next read without restarting the store.
* Template-less or mangled files degrade gracefully without
  raising.

File format contract:
    - ``## Active lessons`` header separates preamble from
      the active bullet list.
    - Bullets are ``- text`` or ``- [category] text``.
    - Empty file / missing header → empty list (with warning).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.lessons import Lesson, LessonsStore, default_lessons_path

# --------------------------------------------------------------- basics


def test_fresh_store_writes_template(tmp_path: Path) -> None:
    """Constructor on a missing file writes the template so the
    operator can see what the file is for. Template is a
    ``read``-only thing until lessons are added."""
    path = tmp_path / "lessons.md"
    LessonsStore(path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# Learned corrections" in content
    assert "## Active lessons" in content


def test_fresh_store_reads_empty(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    assert store.read() == []


def test_render_block_is_empty_without_lessons(tmp_path: Path) -> None:
    """When the file has no lessons, ``render_block`` returns
    the empty string so the memory store can suppress the
    whole ``[Learned corrections]`` block in the system
    prompt."""
    store = LessonsStore(tmp_path / "lessons.md")
    assert store.render_block() == ""


def test_render_block_has_header_and_bullets(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    store.add("always use uv, not pip")
    store.add("prefer git_commit over staging", category="tooling")
    block = store.render_block()
    assert block.startswith("[Learned corrections]\n\n")
    assert "- always use uv, not pip" in block
    assert "- [tooling] prefer git_commit over staging" in block


# --------------------------------------------------------------- add / remove


def test_add_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "lessons.md"
    store = LessonsStore(path)
    store.add("always use uv, not pip")
    # Reload via a fresh store → same entries.
    fresh = LessonsStore(path)
    lessons = fresh.read()
    assert [lsn.text for lsn in lessons] == ["always use uv, not pip"]


def test_add_with_category_persists_category(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    store.add("prefer ruff format", category="tooling")
    lessons = store.read()
    assert len(lessons) == 1
    assert lessons[0].category == "tooling"
    assert lessons[0].text == "prefer ruff format"


def test_add_empty_text_rejected(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    with pytest.raises(ValueError, match="non-empty"):
        store.add("")
    with pytest.raises(ValueError, match="non-empty"):
        store.add("   \n\t  ")


def test_add_strips_whitespace(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    store.add("  always use uv  ")
    lessons = store.read()
    assert lessons[0].text == "always use uv"


def test_remove_substring_case_insensitive(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    store.add("always use uv, not pip")
    store.add("UV works on Windows too")
    store.add("prefer ruff format")
    removed = store.remove("uv")
    assert removed == 2
    remaining = [lsn.text for lsn in store.read()]
    assert remaining == ["prefer ruff format"]


def test_remove_no_match_returns_zero(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    store.add("always use uv")
    assert store.remove("nonexistent") == 0
    assert len(store.read()) == 1


def test_remove_empty_substring_is_noop(tmp_path: Path) -> None:
    """An empty or whitespace-only substring must NOT match
    everything — that'd let a bad tool call wipe the whole
    file. Guard against it with a zero-count return and no
    mutation."""
    store = LessonsStore(tmp_path / "lessons.md")
    store.add("always use uv")
    assert store.remove("") == 0
    assert store.remove("   ") == 0
    assert len(store.read()) == 1


# --------------------------------------------------------------- ceiling


def test_max_entries_drops_oldest(tmp_path: Path) -> None:
    """Past ``max_entries``, oldest (first-added) is dropped."""
    store = LessonsStore(tmp_path / "lessons.md", max_entries=3)
    store.add("lesson one")
    store.add("lesson two")
    store.add("lesson three")
    store.add("lesson four")
    texts = [lsn.text for lsn in store.read()]
    assert texts == ["lesson two", "lesson three", "lesson four"]


def test_max_entries_default_is_fifty(tmp_path: Path) -> None:
    store = LessonsStore(tmp_path / "lessons.md")
    assert store.max_entries == 50


# --------------------------------------------------------------- mtime freshness


def test_external_edit_picked_up_on_next_read(tmp_path: Path) -> None:
    """An operator editing the file in $EDITOR should see the
    edit reflected on the next request without restart."""
    import os
    import time

    path = tmp_path / "lessons.md"
    store = LessonsStore(path)
    store.add("original")
    assert [lsn.text for lsn in store.read()] == ["original"]

    # Rewrite the file externally with different content.
    # Need to bump mtime so the cache detects the change.
    content = path.read_text(encoding="utf-8")
    content = content.replace("original", "externally-edited")
    path.write_text(content, encoding="utf-8")
    # On some filesystems two writes within the same ns tick
    # produce identical mtime. Force-advance via utime.
    later = time.time() + 2
    os.utime(path, (later, later))

    assert [lsn.text for lsn in store.read()] == ["externally-edited"]


def test_deleted_file_regenerates_template(tmp_path: Path) -> None:
    """If the operator deletes the file, the next read
    rewrites the template and returns an empty list rather
    than raising."""
    path = tmp_path / "lessons.md"
    store = LessonsStore(path)
    store.add("keep me")
    path.unlink()
    assert store.read() == []
    assert path.exists()
    assert "## Active lessons" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------- parser edge cases


def test_missing_header_returns_empty_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A file that's been edited into a shape without the
    ``## Active lessons`` header shouldn't crash; it should
    log a warning and return an empty list."""
    path = tmp_path / "lessons.md"
    path.write_text("# Some weird file\n\njust prose, no header", encoding="utf-8")
    store = LessonsStore(path)
    with caplog.at_level("WARNING"):
        assert store.read() == []
    assert any("missing_header" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_bullets_before_header_ignored(tmp_path: Path) -> None:
    """Bullets in the preamble (e.g. inside the template's
    prose) must NOT be parsed as lessons. Only bullets after
    ``## Active lessons`` count."""
    path = tmp_path / "lessons.md"
    path.write_text(
        "# Learned corrections\n\n"
        "Some prose.\n\n"
        "- this is a bullet in prose, not a lesson\n\n"
        "## Active lessons\n\n"
        "- this is a real lesson\n",
        encoding="utf-8",
    )
    store = LessonsStore(path)
    texts = [lsn.text for lsn in store.read()]
    assert texts == ["this is a real lesson"]


def test_non_bullet_lines_in_active_section_ignored(tmp_path: Path) -> None:
    """Comments and blank lines interleaved with bullets are
    allowed and ignored."""
    path = tmp_path / "lessons.md"
    path.write_text(
        "# Learned corrections\n\n"
        "## Active lessons\n\n"
        "- first lesson\n\n"
        "Some interspersed comment the operator added.\n\n"
        "- second lesson\n",
        encoding="utf-8",
    )
    store = LessonsStore(path)
    texts = [lsn.text for lsn in store.read()]
    assert texts == ["first lesson", "second lesson"]


def test_unreadable_file_returns_empty_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A read-time OSError (permission denied, disk error)
    should be logged and return empty — NOT raise, because
    the store is on the system-prompt-building path."""
    path = tmp_path / "lessons.md"
    path.write_text("# x\n## Active lessons\n- hi\n", encoding="utf-8")
    store = LessonsStore(path)

    original_read_text = Path.read_text

    def _blow_up(self: Path, *a: object, **kw: object) -> str:
        if self == path:
            raise OSError("permission denied")
        return original_read_text(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", _blow_up)
    # Force a mtime bump so the cache-reload path actually hits disk.
    import os
    import time as _time

    later = _time.time() + 2
    os.utime(path, (later, later))
    with caplog.at_level("WARNING"):
        assert store.read() == []
    assert any("read_failed" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


# --------------------------------------------------------------- Lesson dataclass


def test_lesson_render_without_category() -> None:
    assert Lesson(text="foo").render() == "- foo"


def test_lesson_render_with_category() -> None:
    assert Lesson(text="foo", category="tooling").render() == "- [tooling] foo"


def test_lesson_from_bullet_without_category() -> None:
    lsn = Lesson.from_bullet("  just some text  ")
    assert lsn.text == "just some text"
    assert lsn.category is None


def test_lesson_from_bullet_with_category() -> None:
    lsn = Lesson.from_bullet("[tooling] prefer uv")
    assert lsn.text == "prefer uv"
    assert lsn.category == "tooling"


def test_lesson_from_bullet_malformed_category_degrades() -> None:
    """``[unclosed lesson`` isn't really a category; fall
    back to treating the whole body as the text so we don't
    lose information from a hand-edited entry."""
    lsn = Lesson.from_bullet("[unclosed lesson")
    assert lsn.text == "[unclosed lesson"
    assert lsn.category is None


# --------------------------------------------------------------- default path


def test_default_lessons_path(tmp_path: Path) -> None:
    assert default_lessons_path(tmp_path) == tmp_path / "lessons.md"

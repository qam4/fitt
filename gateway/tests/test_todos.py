"""Tests for Phase E — TodoStore.

Unit-level coverage of the store behaviours:

* add / list / done / remove round-trip through the file.
* open_todos filters out completed items.
* Max-entries ceiling drops oldest when exceeded.
* mtime-based freshness: an external edit is picked up on the next
  read without restarting the store.
* Deleted / mangled files degrade gracefully without raising.

File format contract:
    - ``## Open`` header separates preamble from the checkbox list.
    - Items are markdown checkboxes: ``- [ ] open`` / ``- [x] done``.
    - Bullets before the header are ignored.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.todos import Todo, TodoStore, default_todos_path

# --------------------------------------------------------------- basics


def test_fresh_store_writes_template(tmp_path: Path) -> None:
    path = tmp_path / "todos.md"
    TodoStore(path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# Todos" in content
    assert "## Open" in content


def test_fresh_store_reads_empty(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    assert store.read() == []
    assert store.open_todos() == []


# --------------------------------------------------------------- add / list


def test_add_persists_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "todos.md"
    store = TodoStore(path)
    store.add("call the doctor")
    # Reload via a fresh store → same entries.
    fresh = TodoStore(path)
    todos = fresh.read()
    assert [t.text for t in todos] == ["call the doctor"]
    assert todos[0].done is False


def test_add_empty_text_rejected(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    with pytest.raises(ValueError, match="non-empty"):
        store.add("")
    with pytest.raises(ValueError, match="non-empty"):
        store.add("   \n\t  ")


def test_add_strips_whitespace(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("  call the doctor  ")
    assert store.read()[0].text == "call the doctor"


def test_add_returns_the_todo(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    todo = store.add("buy milk")
    assert todo.text == "buy milk"
    assert todo.done is False


# --------------------------------------------------------------- done


def test_mark_done_flips_matching_open(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("call the doctor")
    store.add("buy milk")
    flipped = store.mark_done("doctor")
    assert flipped == 1
    todos = {t.text: t.done for t in store.read()}
    assert todos == {"call the doctor": True, "buy milk": False}


def test_mark_done_is_case_insensitive(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("Call the Doctor")
    assert store.mark_done("DOCTOR") == 1


def test_mark_done_skips_already_done(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("call the doctor")
    assert store.mark_done("doctor") == 1
    # Second time: nothing open matches.
    assert store.mark_done("doctor") == 0


def test_mark_done_no_match_returns_zero(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("buy milk")
    assert store.mark_done("nonexistent") == 0


def test_mark_done_empty_substring_is_noop(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("buy milk")
    assert store.mark_done("") == 0
    assert store.mark_done("   ") == 0
    assert store.open_todos()[0].done is False


def test_open_todos_excludes_done(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("call the doctor")
    store.add("buy milk")
    store.mark_done("doctor")
    assert [t.text for t in store.open_todos()] == ["buy milk"]


# --------------------------------------------------------------- remove


def test_remove_substring_case_insensitive(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("call the doctor")
    store.add("Doctor Who marathon")
    store.add("buy milk")
    removed = store.remove("doctor")
    assert removed == 2
    assert [t.text for t in store.read()] == ["buy milk"]


def test_remove_no_match_returns_zero(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("buy milk")
    assert store.remove("nonexistent") == 0
    assert len(store.read()) == 1


def test_remove_empty_substring_is_noop(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    store.add("buy milk")
    assert store.remove("") == 0
    assert store.remove("   ") == 0
    assert len(store.read()) == 1


# --------------------------------------------------------------- ceiling


def test_max_entries_drops_oldest(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md", max_entries=3)
    store.add("one")
    store.add("two")
    store.add("three")
    store.add("four")
    assert [t.text for t in store.read()] == ["two", "three", "four"]


def test_max_entries_default(tmp_path: Path) -> None:
    store = TodoStore(tmp_path / "todos.md")
    assert store._max_entries == 200


# --------------------------------------------------------------- freshness


def test_external_edit_picked_up_on_next_read(tmp_path: Path) -> None:
    import os
    import time

    path = tmp_path / "todos.md"
    store = TodoStore(path)
    store.add("original")
    assert [t.text for t in store.read()] == ["original"]

    content = path.read_text(encoding="utf-8")
    content = content.replace("original", "externally-edited")
    path.write_text(content, encoding="utf-8")
    later = time.time() + 2
    os.utime(path, (later, later))

    assert [t.text for t in store.read()] == ["externally-edited"]


def test_deleted_file_regenerates_template(tmp_path: Path) -> None:
    path = tmp_path / "todos.md"
    store = TodoStore(path)
    store.add("keep me")
    path.unlink()
    assert store.read() == []
    assert path.exists()
    assert "## Open" in path.read_text(encoding="utf-8")


# --------------------------------------------------------------- parser edges


def test_bullets_before_header_ignored(tmp_path: Path) -> None:
    path = tmp_path / "todos.md"
    path.write_text(
        "# Todos\n\n"
        "Some prose.\n\n"
        "- [ ] this is a bullet in prose, not a todo\n\n"
        "## Open\n\n"
        "- [ ] a real todo\n",
        encoding="utf-8",
    )
    store = TodoStore(path)
    assert [t.text for t in store.read()] == ["a real todo"]


def test_done_items_parsed(tmp_path: Path) -> None:
    path = tmp_path / "todos.md"
    path.write_text(
        "# Todos\n\n## Open\n\n- [ ] open one\n- [x] done one\n",
        encoding="utf-8",
    )
    store = TodoStore(path)
    todos = store.read()
    assert todos[0] == Todo(text="open one", done=False)
    assert todos[1] == Todo(text="done one", done=True)


def test_non_bullet_lines_ignored(tmp_path: Path) -> None:
    path = tmp_path / "todos.md"
    path.write_text(
        "# Todos\n\n## Open\n\n- [ ] first\n\nsome interspersed comment\n\n- [ ] second\n",
        encoding="utf-8",
    )
    store = TodoStore(path)
    assert [t.text for t in store.read()] == ["first", "second"]


# --------------------------------------------------------------- Todo dataclass


def test_todo_render_open() -> None:
    assert Todo(text="foo").render() == "- [ ] foo"


def test_todo_render_done() -> None:
    assert Todo(text="foo", done=True).render() == "- [x] foo"


def test_todo_from_bullet_open() -> None:
    t = Todo.from_bullet(" ", "  buy milk  ")
    assert t == Todo(text="buy milk", done=False)


def test_todo_from_bullet_done() -> None:
    t = Todo.from_bullet("x", "buy milk")
    assert t == Todo(text="buy milk", done=True)


# --------------------------------------------------------------- default path


def test_default_todos_path(tmp_path: Path) -> None:
    assert default_todos_path(tmp_path) == tmp_path / "todos.md"

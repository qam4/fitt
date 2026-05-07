"""Unit tests for the memory store.

Covers the markdown turn format, truncation, identity defaults, and
the property tests from design.md.
"""

from __future__ import annotations

import string
from datetime import date
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.memory import MemoryStore, _parse_turns
from gateway.memory_templates import DEFAULTS, LEGACY_TEMPLATES


def _store(tmp_path: Path, **overrides) -> MemoryStore:
    kwargs = {
        "identity_dir": tmp_path / "identity",
        "sessions_dir": tmp_path / "sessions",
        "max_history_chars": 24_000,
        "enabled": True,
    }
    kwargs.update(overrides)
    return MemoryStore(**kwargs)


# ---------- identity defaults -------------------------------------


def test_identity_templates_created_on_first_load(tmp_path: Path) -> None:
    assert not (tmp_path / "identity").exists()
    _store(tmp_path)
    for name in DEFAULTS:
        assert (tmp_path / "identity" / name).exists()


def test_identity_templates_not_overwritten(tmp_path: Path) -> None:
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "user.md").write_text("custom user content", encoding="utf-8")
    _store(tmp_path)
    assert (ident / "user.md").read_text(encoding="utf-8") == "custom user content"


def test_identity_edits_picked_up_without_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx_1 = store.load_context("main")
    # Edit user.md to something distinct.
    (tmp_path / "identity" / "user.md").write_text(
        "# About Me\n\nI love chess.\n", encoding="utf-8"
    )
    ctx_2 = store.load_context("main")
    assert "chess" in ctx_2.system_prefix
    assert "chess" not in ctx_1.system_prefix


def test_identity_missing_file_tolerated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # Remove tools.md after store init
    (tmp_path / "identity" / "tools.md").unlink()
    ctx = store.load_context("main")
    # user and soul still present
    assert "About Me" in ctx.system_prefix or "TODO" in ctx.system_prefix


# ---------- legacy-default heal -----------------------------------


def test_identity_heals_legacy_verbatim_default(tmp_path: Path) -> None:
    """A tools.md still carrying the retired "you have no tools"
    Phase 2 default gets overwritten with the current template
    on boot. Existing installs auto-repair without the operator
    having to delete the file."""
    ident = tmp_path / "identity"
    ident.mkdir()
    legacy = LEGACY_TEMPLATES["tools.md"][0]
    (ident / "tools.md").write_text(legacy, encoding="utf-8")
    # Seed the other defaults so the store doesn't create them
    # during init and claim that as the heal.
    (ident / "user.md").write_text(DEFAULTS["user.md"], encoding="utf-8")
    (ident / "soul.md").write_text(DEFAULTS["soul.md"], encoding="utf-8")

    _store(tmp_path)

    content = (ident / "tools.md").read_text(encoding="utf-8")
    assert content == DEFAULTS["tools.md"]
    # Sanity: the retired text, which actively misled the model
    # with "you do NOT have tool access", is gone.
    assert "do NOT have tool access" not in content


def test_identity_heal_leaves_operator_edits_alone(tmp_path: Path) -> None:
    """Any content that isn't a byte-for-byte legacy match is
    treated as operator edit — the heal path must not touch it.
    This is the invariant that keeps the heal safe to ship."""
    ident = tmp_path / "identity"
    ident.mkdir()
    custom = "# My tools\n\nPrefer uv over pip on every project.\n"
    (ident / "tools.md").write_text(custom, encoding="utf-8")

    _store(tmp_path)

    assert (ident / "tools.md").read_text(encoding="utf-8") == custom


def test_identity_heal_leaves_slightly_modified_legacy_alone(tmp_path: Path) -> None:
    """A file that started as a legacy default but has even one
    character of operator modification is no longer a legacy
    match — heal must skip it. Otherwise a user who tweaked the
    Phase 2 template (e.g. added a note) would lose their edit.

    Byte-for-byte equality is the guard. Trust the hash."""
    ident = tmp_path / "identity"
    ident.mkdir()
    legacy = LEGACY_TEMPLATES["tools.md"][0]
    modified = legacy + "\n\n(my notes here)\n"
    (ident / "tools.md").write_text(modified, encoding="utf-8")

    _store(tmp_path)

    assert (ident / "tools.md").read_text(encoding="utf-8") == modified


def test_identity_heal_runs_only_on_listed_files(tmp_path: Path) -> None:
    """LEGACY_TEMPLATES is scoped by file name. A legacy-looking
    tools.md body sitting in user.md must not be healed — the
    per-file list is the whole point."""
    ident = tmp_path / "identity"
    ident.mkdir()
    legacy = LEGACY_TEMPLATES["tools.md"][0]
    (ident / "user.md").write_text(legacy, encoding="utf-8")

    _store(tmp_path)

    # user.md still holds the (weird, but not operator-validated)
    # content — no file-type confusion in the heal logic.
    assert (ident / "user.md").read_text(encoding="utf-8") == legacy


def test_new_tools_template_defers_to_capability_block(tmp_path: Path) -> None:
    """Pin the intent of the new template: it tells the model to
    trust the live ``[Capabilities]`` block over the file.

    If someone rewrites tools.md in a way that re-introduces the
    "I have no tools" misdirection, this test fails loudly."""
    _store(tmp_path)
    content = (tmp_path / "identity" / "tools.md").read_text(encoding="utf-8")
    assert "[Capabilities]" in content
    # Reject any framing that asserts a universal "no tools"
    # stance; the whole point of the heal is to stop the model
    # reading that prose.
    assert "do NOT have tool access" not in content
    assert "Phase 4" not in content  # stale roadmap reference from the old template


def test_disabled_returns_empty_context(tmp_path: Path) -> None:
    store = _store(tmp_path, enabled=False)
    # When disabled, identity dir shouldn't even be created.
    assert not (tmp_path / "identity").exists()
    ctx = store.load_context("main")
    assert ctx.system_prefix == ""
    assert ctx.history_messages == []


def test_disabled_append_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path, enabled=False)
    store.append_turn("main", "hi", "hello")
    assert not (tmp_path / "sessions").exists()


# ---------- history path + append ---------------------------------


def test_history_path_is_session_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.history_path("main", date(2026, 4, 30))
    assert path == tmp_path / "sessions" / "main" / "history" / "2026-04-30.md"


def test_append_turn_creates_parent_dirs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_turn("main", "hi", "hello")
    assert (tmp_path / "sessions" / "main" / "history").is_dir()


def test_append_turn_writes_both_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_turn("main", "hi", "hello")
    content = store.history_path("main").read_text(encoding="utf-8")
    assert "user" in content
    assert "assistant" in content
    assert "hi" in content
    assert "hello" in content


def test_append_multiple_turns_preserves_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_turn("main", "q1", "a1")
    store.append_turn("main", "q2", "a2")
    store.append_turn("main", "q3", "a3")
    ctx = store.load_context("main")
    contents = [m["content"] for m in ctx.history_messages]
    assert contents == ["q1", "a1", "q2", "a2", "q3", "a3"]


def test_append_turn_format_has_iso_timestamp(tmp_path: Path) -> None:
    import re

    store = _store(tmp_path)
    store.append_turn("main", "hi", "hello")
    content = store.history_path("main").read_text(encoding="utf-8")
    # Timestamps look like 2026-04-30T17:42:12Z
    assert re.search(
        r"^## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z (user|assistant)$",
        content,
        re.MULTILINE,
    )


# ---------- load + truncate ---------------------------------------


def test_load_empty_history_returns_empty_list(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ctx = store.load_context("main")
    assert ctx.history_messages == []
    assert ctx.truncated_bytes == 0


def test_load_corrupted_file_tolerated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = store.history_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage content with no headers", encoding="utf-8")
    # Should not raise; should return no turns.
    ctx = store.load_context("main")
    assert ctx.history_messages == []


def test_truncation_drops_oldest(tmp_path: Path) -> None:
    # Budget of 50 chars; each turn carries 20 chars of content.
    store = _store(tmp_path, max_history_chars=50)
    for i in range(5):
        store.append_turn(
            "main",
            f"user-msg-{i:03d}",  # 13 chars
            f"assistant-msg-{i:03d}",  # 18 chars
        )
    ctx = store.load_context("main")
    # Only the most recent turns fit. The loaded contents are a
    # suffix of the full sequence [u0, a0, u1, a1, ..., u4, a4].
    all_contents = [c for i in range(5) for c in (f"user-msg-{i:03d}", f"assistant-msg-{i:03d}")]
    loaded = [m["content"] for m in ctx.history_messages]
    assert loaded == all_contents[-len(loaded) :]
    assert ctx.truncated_bytes > 0


def test_truncation_reports_dropped_bytes(tmp_path: Path) -> None:
    store = _store(tmp_path, max_history_chars=10)
    store.append_turn("main", "some longish message", "a reply here")
    store.append_turn("main", "another longish message", "another reply here")
    ctx = store.load_context("main")
    assert ctx.truncated_bytes > 0


# ---------- parser ------------------------------------------------


def test_parse_turns_recovers_structure() -> None:
    raw = (
        "## 2026-04-30T10:00:00Z user\n\nhello\n\n## 2026-04-30T10:00:01Z assistant\n\nhi there\n\n"
    )
    turns = _parse_turns(raw)
    assert [t.role for t in turns] == ["user", "assistant"]
    assert turns[0].content == "hello"
    assert turns[1].content == "hi there"


def test_parse_turns_permissive_on_preamble() -> None:
    # Text before the first header is ignored (file preamble).
    raw = "random notes\nnot a turn\n## 2026-04-30T10:00:00Z user\n\nactual turn\n"
    turns = _parse_turns(raw)
    assert len(turns) == 1
    assert turns[0].content == "actual turn"


# ---------- property tests (Phase 2, Property 2 + 4) -------------


@given(
    turns=st.lists(
        st.tuples(
            st.text(
                alphabet=string.ascii_letters + string.digits,
                min_size=1,
                max_size=40,
            ),
            st.text(
                alphabet=string.ascii_letters + string.digits,
                min_size=1,
                max_size=40,
            ),
        ),
        min_size=1,
        max_size=20,
    ),
)
@settings(max_examples=50, deadline=2000)
def test_property_history_round_trip_order(tmp_path_factory, turns):
    """Phase 2, Property 2: for any sequence of N turns, round-trip
    order through the store equals the original order.

    Content alphabet excludes whitespace because pure-whitespace
    messages are stripped during parse (intentional - they're
    degenerate turns).
    """
    tmp = tmp_path_factory.mktemp("history-rt")
    store = MemoryStore(
        identity_dir=tmp / "identity",
        sessions_dir=tmp / "sessions",
        max_history_chars=10_000_000,  # no truncation
        enabled=True,
    )
    for u, a in turns:
        store.append_turn("main", u, a)
    ctx = store.load_context("main")
    expected = [m for pair in turns for m in pair]
    actual = [m["content"] for m in ctx.history_messages]
    assert actual == expected


@given(
    n_turns=st.integers(min_value=2, max_value=30),
    budget=st.integers(min_value=10, max_value=500),
    content_len=st.integers(min_value=5, max_value=50),
)
@settings(max_examples=30, deadline=3000)
def test_property_truncation_keeps_suffix(tmp_path_factory, n_turns, budget, content_len):
    """Phase 2, Property 4: loaded slice is always a contiguous
    suffix of the full on-disk turn sequence."""
    tmp = tmp_path_factory.mktemp("trunc-suffix")
    store = MemoryStore(
        identity_dir=tmp / "identity",
        sessions_dir=tmp / "sessions",
        max_history_chars=budget,
        enabled=True,
    )
    full_contents = []
    for i in range(n_turns):
        user_msg = ("u" + str(i).zfill(3)) * (content_len // 4 + 1)
        asst_msg = ("a" + str(i).zfill(3)) * (content_len // 4 + 1)
        store.append_turn("main", user_msg, asst_msg)
        full_contents.extend([user_msg, asst_msg])
    ctx = store.load_context("main")
    loaded = [m["content"] for m in ctx.history_messages]
    # The loaded sequence must be a contiguous tail of full_contents.
    if loaded:
        assert full_contents[-len(loaded) :] == loaded
    # And it must fit the budget (+ a rounding tolerance for partial
    # turn rejection).
    loaded_size = sum(len(c) for c in loaded)
    assert loaded_size <= budget or not loaded

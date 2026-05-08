"""Phase 5 — decaying history injection.

Four layers:

1. **Today**: full turns, same as Phase 2.
2. **Yesterday**: first turn + count marker.
3. **3-30 days ago**: one-line summary per day.
4. **30+ days**: dropped.

Tests pin each layer's behaviour with seeded files per day
and explicit ``now=`` overrides so we don't race the wall
clock.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from gateway.memory import MemoryStore


def _make_store(tmp_path: Path, *, budget: int = 24_000) -> MemoryStore:
    identity_dir = tmp_path / "identity"
    sessions_dir = tmp_path / "sessions"
    identity_dir.mkdir()
    return MemoryStore(
        identity_dir=identity_dir,
        sessions_dir=sessions_dir,
        max_history_chars=budget,
        enabled=True,
    )


def _seed_day(
    store: MemoryStore,
    session: str,
    day: date,
    turns: list[tuple[str, str]],
) -> None:
    """Write a seed file for ``day`` with the given turns.

    ``turns`` is a list of ``(role, content)``. Role accepts
    ``user``, ``assistant``, or the Phase 5 extensions
    ``assistant tool_calls`` / ``tool <name>``.
    """
    path = store.history_path(session, day=day)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    ts_base = f"{day.isoformat()}T10:00:00Z"
    for idx, (role, content) in enumerate(turns):
        # Fake up a per-turn timestamp; content doesn't matter
        # for the test as long as the header parses.
        ts = f"{day.isoformat()}T10:{idx:02d}:00Z" if idx < 60 else ts_base
        lines.append(f"## {ts} {role}\n\n{content}\n")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------- today


def test_today_full(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    _seed_day(
        store,
        "main",
        today,
        [
            ("user", "hello"),
            ("assistant", "hi there"),
            ("user", "what's up"),
            ("assistant", "nothing much"),
        ],
    )
    messages, _ = store._load_decaying_history("main", now=today)
    assert [m["role"] for m in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[0]["content"] == "hello"
    assert messages[-1]["content"] == "nothing much"


# --------------------------------------------------------------- yesterday


def test_yesterday_first_turn_plus_count(tmp_path: Path) -> None:
    """Yesterday contributes the first user+assistant pair plus
    a system marker counting the remaining user turns."""
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    yesterday = date(2026, 5, 7)
    _seed_day(
        store,
        "main",
        yesterday,
        [
            ("user", "first q"),
            ("assistant", "first a"),
            ("user", "second q"),
            ("assistant", "second a"),
            ("user", "third q"),
            ("assistant", "third a"),
        ],
    )
    messages, _ = store._load_decaying_history("main", now=today)
    # First pair kept verbatim.
    contents = [m["content"] for m in messages]
    assert "first q" in contents
    assert "first a" in contents
    # Second and third user turns NOT persisted — replaced by
    # a system count marker.
    assert "second q" not in contents
    assert "third q" not in contents
    # Marker mentions 2 more user turns.
    markers = [m for m in messages if m["role"] == "system"]
    assert any("Yesterday" in m["content"] and "2 more" in m["content"] for m in markers)


def test_yesterday_single_turn_no_marker(tmp_path: Path) -> None:
    """When yesterday had only one user turn, the whole day
    IS the first turn — no count marker."""
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    yesterday = date(2026, 5, 7)
    _seed_day(
        store,
        "main",
        yesterday,
        [
            ("user", "only q"),
            ("assistant", "only a"),
        ],
    )
    messages, _ = store._load_decaying_history("main", now=today)
    markers = [m for m in messages if m["role"] == "system" and "Yesterday" in m.get("content", "")]
    assert markers == []


def test_yesterday_missing_file_noop(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    # Only today exists, yesterday's file absent.
    _seed_day(
        store,
        "main",
        today,
        [("user", "hi"), ("assistant", "hello")],
    )
    messages, _ = store._load_decaying_history("main", now=today)
    # Only today's messages.
    assert [m["role"] for m in messages] == ["user", "assistant"]


# --------------------------------------------------------------- 3-30 days ago


def test_past_activity_summary_renders_per_day(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    # Three days ago.
    _seed_day(
        store,
        "main",
        date(2026, 5, 5),
        [("user", "old1"), ("assistant", "reply1"), ("user", "old2"), ("assistant", "reply2")],
    )
    # Ten days ago.
    _seed_day(
        store,
        "main",
        date(2026, 4, 28),
        [("user", "way older"), ("assistant", "r")],
    )
    # Thirty-one days ago — should NOT appear.
    _seed_day(
        store,
        "main",
        date(2026, 4, 7),
        [("user", "too old"), ("assistant", "r")],
    )

    messages, _ = store._load_decaying_history("main", now=today)
    summaries = [
        m for m in messages if m["role"] == "system" and "[Past activity]" in m.get("content", "")
    ]
    assert len(summaries) == 1
    body = summaries[0]["content"]
    # Three-days-ago has 2 user turns.
    assert "2026-05-05: 2 user turns (chat only)" in body
    # Ten-days-ago has 1.
    assert "2026-04-28: 1 user turns (chat only)" in body
    # Thirty-one-days-ago dropped.
    assert "2026-04-07" not in body


def test_past_activity_summary_detects_tools_used(tmp_path: Path) -> None:
    """A day with tool-using turns renders as 'with tools'
    rather than 'chat only'."""
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    _seed_day(
        store,
        "main",
        date(2026, 5, 5),
        [
            ("user", "do the thing"),
            ("assistant tool_calls", "- read_file(path='x')"),
            ("tool read_file", "ok"),
            ("assistant", "done"),
        ],
    )
    messages, _ = store._load_decaying_history("main", now=today)
    summaries = [
        m for m in messages if m["role"] == "system" and "[Past activity]" in m.get("content", "")
    ]
    assert "(with tools)" in summaries[0]["content"]


def test_past_activity_summary_skips_empty_days(tmp_path: Path) -> None:
    """A day with no user turns doesn't show up in the summary."""
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    # An empty file.
    path = store.history_path("main", day=date(2026, 5, 5))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    messages, _ = store._load_decaying_history("main", now=today)
    summaries = [
        m for m in messages if m["role"] == "system" and "[Past activity]" in m.get("content", "")
    ]
    assert summaries == []


# --------------------------------------------------------------- ordering


def test_layer_ordering(tmp_path: Path) -> None:
    """Assembled message list ordering: past summary → yesterday
    → today. A model reading top-down should see the chronology
    as "long ago, recently, just now."""
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    _seed_day(
        store,
        "main",
        date(2026, 4, 30),
        [("user", "days ago"), ("assistant", "r")],
    )
    _seed_day(
        store,
        "main",
        date(2026, 5, 7),
        [("user", "yesterday q"), ("assistant", "yesterday a")],
    )
    _seed_day(
        store,
        "main",
        today,
        [("user", "today q"), ("assistant", "today a")],
    )

    messages, _ = store._load_decaying_history("main", now=today)
    content_order = [m.get("content", "") for m in messages]
    past_idx = next(i for i, c in enumerate(content_order) if "[Past activity]" in c)
    yesterday_idx = next(i for i, c in enumerate(content_order) if "yesterday q" in c)
    today_idx = next(i for i, c in enumerate(content_order) if "today q" in c)
    assert past_idx < yesterday_idx < today_idx


# --------------------------------------------------------------- budget


def test_budget_drops_oldest_first(tmp_path: Path) -> None:
    """When the assembled messages exceed the budget, the past
    summary goes before yesterday, yesterday before today's
    oldest turns."""
    # Tiny budget so even one layer overflows.
    store = _make_store(tmp_path, budget=50)
    today = date(2026, 5, 8)

    _seed_day(
        store,
        "main",
        date(2026, 5, 5),
        [("user", "ancient"), ("assistant", "ok")],
    )
    _seed_day(
        store,
        "main",
        date(2026, 5, 7),
        [("user", "yesterday q"), ("assistant", "yesterday a")],
    )
    _seed_day(
        store,
        "main",
        today,
        [("user", "today q"), ("assistant", "today a")],
    )

    messages, dropped = store._load_decaying_history("main", now=today)
    # Budget squeeze: past summary should have been dropped
    # first. "ancient" mention disappears.
    rendered = "\n".join(m.get("content", "") for m in messages)
    assert "[Past activity]" not in rendered
    assert dropped > 0


# --------------------------------------------------------------- integration via load_context


def test_load_context_includes_decay(tmp_path: Path) -> None:
    """``load_context`` (the public API) uses decay internally.
    A test that seeds yesterday + today and asserts the decay
    layer appears in the returned LoadedContext."""
    store = _make_store(tmp_path)
    today = date(2026, 5, 8)
    yesterday = date(2026, 5, 7)
    _seed_day(
        store,
        "main",
        yesterday,
        [
            ("user", "y1"),
            ("assistant", "ya"),
            ("user", "y2"),
            ("assistant", "yb"),
        ],
    )
    _seed_day(
        store,
        "main",
        today,
        [("user", "t1"), ("assistant", "ta")],
    )

    # Monkeypatch _today so load_context's call flows through
    # our fixed "today." Since it's a module-level helper we
    # poke directly.
    import gateway.memory as _mem

    original = _mem._today
    try:
        _mem._today = lambda: today  # type: ignore[assignment]
        ctx = store.load_context("main")
    finally:
        _mem._today = original

    roles = [m["role"] for m in ctx.history_messages]
    assert "system" in roles  # yesterday's count marker
    contents = [m.get("content", "") for m in ctx.history_messages]
    assert any("y1" in c for c in contents)
    assert any("t1" in c for c in contents)

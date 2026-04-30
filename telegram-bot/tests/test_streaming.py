"""Tests for the StreamingEditor's rate-limited edit behaviour."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fitt_telegram_bot.streaming import StreamingEditor


@dataclass
class FakeBot:
    """Captures edit_message_text calls."""

    edits: list[tuple[int, int, str]] = field(default_factory=list)
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append((chat_id, message_id, text))

    async def send_message(self, *, chat_id: int, text: str) -> Any:
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": len(self.sent)})()


async def test_append_rate_limited_flushes_first_then_buffers() -> None:
    """The first append always flushes (so the user sees something
    quickly); subsequent appends within the rate-limit window are
    buffered until finalize or the window elapses."""
    bot = FakeBot()
    editor = StreamingEditor(
        bot=bot,
        chat_id=100,
        message_id=7,
        min_interval_s=10,  # long interval so only the first edit goes through
    )
    for delta in ("a", "b", "c"):
        await editor.append(delta)
    # First append flushes immediately (with "a"). The next two are
    # buffered because less than 10s has elapsed.
    assert bot.edits == [(100, 7, "a")]
    await editor.finalize()
    # finalize forces one more edit with the full text.
    assert bot.edits[-1] == (100, 7, "abc")


async def test_append_no_edits_until_buffer_has_content() -> None:
    bot = FakeBot()
    editor = StreamingEditor(bot=bot, chat_id=1, message_id=2, min_interval_s=0)
    await editor.append("")
    assert bot.edits == []
    await editor.finalize()
    # Nothing was ever appended - finalize substitutes (no content).
    assert bot.edits[-1][2] == "(no content)"


async def test_append_truncates_to_4000_chars() -> None:
    bot = FakeBot()
    editor = StreamingEditor(bot=bot, chat_id=1, message_id=2, min_interval_s=0)
    await editor.append("x" * 5000)
    await editor.finalize()
    last = bot.edits[-1][2]
    assert len(last) == 4000
    assert last == "x" * 4000


async def test_append_edits_aggregate_on_finalize() -> None:
    bot = FakeBot()
    editor = StreamingEditor(bot=bot, chat_id=42, message_id=77, min_interval_s=0)
    for d in ["The ", "quick ", "brown ", "fox."]:
        await editor.append(d)
    await editor.finalize()
    # Last edit must contain the fully concatenated text.
    assert bot.edits[-1][2] == "The quick brown fox."

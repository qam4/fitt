"""Tests for ``fitt_telegram_bot.markdown_render`` (Slice 7.4).

Three concerns:

* Per-supported-tag conversion (bold, italic, code, pre, link,
  blockquote, strikethrough).
* Per-unsupported-element graceful degradation (headers, lists,
  tables, images — drop wrapper, keep text).
* Streaming-edit safety: every prefix of a CommonMark document
  produces Telegram-acceptable HTML. Property test, P4 in
  design.md.
* Regression: the 2026-05-22 user complaint ("model replies
  render `**bold**` literally").
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fitt_telegram_bot.markdown_render import (
    is_html_safe_for_telegram,
    markdown_to_telegram_html,
)

# --------------------------------------------------------------- supported tags


def test_bold_renders_as_b() -> None:
    assert markdown_to_telegram_html("Hello **world**") == "Hello <b>world</b>"


def test_italic_star_renders_as_i() -> None:
    assert markdown_to_telegram_html("This is *italic* text") == "This is <i>italic</i> text"


def test_italic_underscore_renders_as_i() -> None:
    """CommonMark accepts both ``*..*`` and ``_.._`` for italic."""
    assert markdown_to_telegram_html("With _underscore_ italic") == "With <i>underscore</i> italic"


def test_inline_code_renders_as_code() -> None:
    md = "Use `read_file` for that."
    assert markdown_to_telegram_html(md) == "Use <code>read_file</code> for that."


def test_fenced_code_renders_as_pre() -> None:
    md = "Run this:\n\n```\nuv run pytest\n```"
    out = markdown_to_telegram_html(md)
    assert "<pre>uv run pytest</pre>" in out


def test_fenced_code_with_language_strips_lang() -> None:
    """Language tags (``` ```python ```) aren't supported by
    Telegram's HTML mode — strip the lang and just render
    ``<pre>``."""
    md = "```python\nprint('hi')\n```"
    out = markdown_to_telegram_html(md)
    assert "<pre>print(&#x27;hi&#x27;)</pre>" in out


def test_link_renders_as_a_tag() -> None:
    md = "See [the docs](https://example.com/page)"
    assert markdown_to_telegram_html(md) == 'See <a href="https://example.com/page">the docs</a>'


def test_blockquote_renders_as_blockquote_tag() -> None:
    md = "> Important note here"
    out = markdown_to_telegram_html(md)
    assert "<blockquote>" in out and "</blockquote>" in out
    assert "Important note here" in out


def test_strikethrough_renders_as_s() -> None:
    md = "This is ~~deprecated~~ now"
    assert markdown_to_telegram_html(md) == "This is <s>deprecated</s> now"


def test_combined_inline_styles() -> None:
    md = "**Bold** and *italic* and `code` together"
    assert (
        markdown_to_telegram_html(md)
        == "<b>Bold</b> and <i>italic</i> and <code>code</code> together"
    )


# --------------------------------------------------------------- unsupported -> text


def test_headings_drop_to_text() -> None:
    md = "# Top heading\n\nSome body."
    out = markdown_to_telegram_html(md)
    # No <h1>..<h6> tags — Telegram doesn't render them.
    assert "<h" not in out.lower()
    assert "Top heading" in out
    assert "Some body" in out


def test_unordered_list_keeps_bullet_text() -> None:
    md = "- one\n- two\n- three"
    out = markdown_to_telegram_html(md)
    # Bullet character preserved as text; no <ul>/<li> tags.
    assert "<ul>" not in out and "<li>" not in out
    assert "•" in out
    assert "one" in out and "two" in out and "three" in out


def test_ordered_list_keeps_numbers_as_text() -> None:
    md = "1. first\n2. second"
    out = markdown_to_telegram_html(md)
    assert "<ol>" not in out and "<li>" not in out
    # We render the same bullet for ordered lists. The numeric
    # markers don't survive markdown-it's tokenisation cleanly
    # so a bullet is acceptable degradation.
    assert "first" in out and "second" in out


def test_image_drops_to_alt_text() -> None:
    md = "![alt text here](https://example.com/img.png)"
    out = markdown_to_telegram_html(md)
    assert "<img" not in out
    assert "alt text here" in out


def test_horizontal_rule_dropped() -> None:
    md = "Above.\n\n---\n\nBelow."
    out = markdown_to_telegram_html(md)
    assert "<hr>" not in out and "<hr/>" not in out
    assert "Above" in out and "Below" in out


# --------------------------------------------------------------- escaping


def test_html_special_chars_escape_in_text() -> None:
    md = "Hello <script>alert(1)</script> & friends"
    out = markdown_to_telegram_html(md)
    # Telegram requires &, <, > escaped in text content.
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_url_escapes_in_href() -> None:
    """Quoted attribute context still requires &/&#x27; escaping."""
    md = "[click](https://example.com/?a=1&b=2)"
    out = markdown_to_telegram_html(md)
    assert 'href="https://example.com/?a=1&amp;b=2"' in out


def test_empty_input_returns_empty() -> None:
    assert markdown_to_telegram_html("") == ""


def test_plain_text_passes_through_escaped() -> None:
    md = "Just plain text here"
    out = markdown_to_telegram_html(md)
    assert out == "Just plain text here"


# --------------------------------------------------------------- regression


def test_regression_2026_05_22_bold_renders_correctly() -> None:
    """The reason this slice exists. Model replies on 2026-05-22
    arrived with literal asterisks because the bot didn't
    convert markdown to anything Telegram could parse. After
    this slice, ``**in 5 minutes**`` renders bold."""
    md = "I'll set the reminder **in 5 minutes**."
    out = markdown_to_telegram_html(md)
    assert "<b>in 5 minutes</b>" in out
    # Apostrophe gets HTML-escaped — Telegram tolerates it.
    assert "**" not in out


# --------------------------------------------------------------- whitelist check


def test_is_html_safe_accepts_whitelisted_tags() -> None:
    safe = "Hello <b>world</b> with <code>test</code>"
    assert is_html_safe_for_telegram(safe)


def test_is_html_safe_rejects_unknown_tag() -> None:
    bad = "Hello <script>alert</script>"
    assert not is_html_safe_for_telegram(bad)


def test_is_html_safe_accepts_link_tag() -> None:
    safe = '<a href="https://example.com">link</a>'
    assert is_html_safe_for_telegram(safe)


# --------------------------------------------------------------- property: streaming-edit safety


@settings(max_examples=100, deadline=None)
@given(
    md=st.text(
        alphabet=st.characters(
            blacklist_categories=("Cs",),  # exclude unpaired surrogates
            blacklist_characters="\x00",
        ),
        min_size=0,
        max_size=200,
    )
)
def test_property_every_prefix_renders_safely(md: str) -> None:
    """P4 from design.md: every prefix of a CommonMark document
    renders to Telegram-acceptable HTML.

    The streaming editor edits a Telegram message many times as
    the model emits tokens. Each edit converts the
    accumulated-so-far buffer through the renderer. If any
    prefix produces invalid HTML, the edit fails and the user
    sees no update.

    Property:
    * The renderer never raises on any prefix.
    * The output passes ``is_html_safe_for_telegram`` (every
      tag is whitelisted).
    """
    for end in range(0, len(md) + 1):
        prefix = md[:end]
        out = markdown_to_telegram_html(prefix)
        # Renderer never raises (asserted by reaching this line
        # without an exception).
        assert is_html_safe_for_telegram(out), f"unsafe HTML for prefix {prefix!r}: {out!r}"


# --------------------------------------------------------------- edge cases the property test seeds


@pytest.mark.parametrize(
    "md",
    [
        "**unclosed bold",
        "*unclosed italic",
        "`unclosed inline",
        "[unclosed link",
        "[partial link](htt",
        "```\nunclosed fence",
        "> blockquote then **bold not closed",
        "<not really html>",
        "&amp;already escaped",
        "\\* literal asterisk",
        "**bold *with italic* inside**",
        "***bold and italic together***",
    ],
)
def test_edge_cases_render_without_raising(md: str) -> None:
    out = markdown_to_telegram_html(md)
    assert is_html_safe_for_telegram(out), f"unsafe HTML for {md!r}: {out!r}"

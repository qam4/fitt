"""CommonMark → Telegram HTML conversion (Phase 7 Slice 7.4).

Why HTML, not MarkdownV2
------------------------

Telegram supports three parse modes:

* **Markdown (legacy).** Limited tag set, no nesting, deprecated.
* **MarkdownV2.** Strict. Every special character outside a code
  block (``_*[]()~`>#+-=|{}.!``) must be escaped, and a half-
  written ``*…*`` mid-streaming-edit crashes the parser for the
  *whole* message — Telegram rejects the edit, and the user sees
  no update until the next successful flush.
* **HTML.** Whitelist of tags (``<b>``, ``<i>``, ``<u>``,
  ``<s>``, ``<code>``, ``<pre>``, ``<a>``, ``<blockquote>``,
  ``<tg-spoiler>``). Half-written tags don't crash the parser —
  Telegram either renders the partial structure correctly or
  treats the unfinished tag as text. Safe under streaming
  edits where the bot edits a Telegram message many times as
  the model emits tokens.

The streaming-edit safety property (Slice 7.4 acceptance
criterion 4.3, design.md decision D7) is what forces HTML.

What we render
--------------

* ``**bold**`` → ``<b>bold</b>``
* ``*italic*`` / ``_italic_`` → ``<i>italic</i>``
* ``` `code` ``` → ``<code>code</code>``
* ` ``` fenced code ``` ` → ``<pre>code</pre>``
* ``[text](url)`` → ``<a href="url">text</a>``
* ``> blockquote`` → ``<blockquote>text</blockquote>``
* ``~~strikethrough~~`` → ``<s>strikethrough</s>``

What degrades to text
---------------------

Telegram has no support for these. We strip the wrapper and keep
the text content:

* Headers (``# H1``..``###### H6``)
* Lists (ordered, unordered) — bullets/numbers stay as text
* Tables
* Horizontal rules
* Images (only the alt text survives)

Unsupported elements never block rendering. The whitelist is
positive (only allow what we know Telegram accepts) so a future
markdown extension that introduces a new token kind degrades
to text rather than producing invalid HTML.

Escaping
--------

Telegram's HTML parser requires ``&``, ``<``, ``>`` escaped in
text content. We escape via ``html.escape`` at every text-token
boundary. URLs in ``href`` attributes go through the same
escape — Telegram parses ``href`` as HTML-attribute-quoted, so
``"`` and ``&`` matter there too.

Streaming property
------------------

Property tested via hypothesis (P4 in design.md): every prefix
of a CommonMark document, when converted, produces a Telegram-
acceptable HTML string. Verified against ~100 randomly-
generated docs.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

_log = logging.getLogger(__name__)


# Whitelist of HTML tags Telegram accepts. See
# https://core.telegram.org/bots/api#html-style.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}
)


# Map markdown-it token tags to the Telegram-accepted tag names.
# Only entries we want to render survive the whitelist; everything
# else degrades to text.
_TOKEN_TAG_MAP: dict[str, str] = {
    "strong": "b",
    "em": "i",
    "code_inline": "code",
    "s": "s",  # strikethrough (commonmark-style ~~..~~)
}


# Module-level parser, reused per call. ``commonmark`` preset is
# the conservative starting point — no GFM extensions enabled by
# default. Strikethrough comes from explicit enabling below.
_PARSER: MarkdownIt = MarkdownIt("commonmark").enable("strikethrough")


def markdown_to_telegram_html(markdown: str) -> str:
    """Convert a CommonMark document to Telegram-compatible HTML.

    Rules:

    * Wrap permitted inline elements in their Telegram tags.
    * Drop wrappers for unsupported tokens; preserve their text
      content.
    * Escape ``&``, ``<``, ``>`` in text. URLs in ``href`` go
      through the same escape so a malicious-looking URL can't
      break the attribute-quoted context.
    * Block elements (paragraphs, blockquotes, code blocks,
      headings, lists) are separated by a single newline so the
      Telegram client can render line breaks naturally.

    Returns plain text on parser failure. The renderer never
    raises — the caller (streaming editor, turn renderer, event
    formatter) treats the output as opaque HTML and ships it.
    """
    if not markdown:
        return ""
    try:
        tokens = _PARSER.parse(markdown)
    except Exception as exc:
        # markdown-it is a pure-Python parser; raises are rare
        # but theoretically possible on adversarial input.
        # Degrade to escaped plaintext rather than failing the
        # message edit.
        _log.warning(
            "markdown_render.parse_failed",
            extra={"error": f"{type(exc).__name__}: {exc}"},
        )
        return html.escape(markdown)

    out: list[str] = []
    _render_tokens(tokens, out)
    return "".join(out).strip()


def _render_tokens(tokens: list[Token], out: list[str]) -> None:
    """Walk a flat token stream produced by markdown-it.

    The token stream is flat — block tokens come in
    open/close pairs around their inline children, plus
    ``inline`` tokens that carry their own ``children`` array
    of further tokens. We recurse into those.
    """
    for tok in tokens:
        _render_token(tok, out)


def _render_token(tok: Token, out: list[str]) -> None:
    t = tok.type

    # Inline container — recurse into its children, which carry
    # the actual ``text``/``strong_open``/etc. tokens.
    if t == "inline":
        if tok.children:
            _render_tokens(tok.children, out)
        return

    # Plain text — escape and emit.
    if t == "text":
        out.append(html.escape(tok.content))
        return

    # Soft and hard line breaks.
    if t in ("softbreak", "hardbreak"):
        out.append("\n")
        return

    # Block-level structures we mostly ignore the wrappers for
    # but emit a newline at close so paragraphs separate.
    if t == "paragraph_open":
        return
    if t == "paragraph_close":
        out.append("\n")
        return

    # Headings: drop the ``#`` markers, render the text content
    # with line breaks around it. Telegram doesn't render heading
    # styles — bold-italic at most could simulate, but it's
    # honest to just drop the styling.
    if t.startswith("heading_open"):
        return
    if t.startswith("heading_close"):
        out.append("\n")
        return

    # Inline emphasis. Map markdown-it's tag name to a Telegram
    # tag via ``_TOKEN_TAG_MAP``. Unmapped tokens drop the
    # wrapper.
    if t in ("strong_open", "em_open", "s_open"):
        tg_tag = _TOKEN_TAG_MAP.get(tok.tag)
        if tg_tag is not None:
            out.append(f"<{tg_tag}>")
        return
    if t in ("strong_close", "em_close", "s_close"):
        tg_tag = _TOKEN_TAG_MAP.get(tok.tag)
        if tg_tag is not None:
            out.append(f"</{tg_tag}>")
        return

    # Inline code. ``code_inline`` is NOT a paired open/close —
    # it's a single token whose ``content`` field holds the
    # code text.
    if t == "code_inline":
        out.append(f"<code>{html.escape(tok.content)}</code>")
        return

    # Fenced code blocks (``` ``` ```) and indented code blocks.
    # Both are single ``fence`` / ``code_block`` tokens with
    # ``content``.
    if t in ("fence", "code_block"):
        out.append(f"<pre>{html.escape(tok.content.rstrip(chr(10)))}</pre>\n")
        return

    # Links: open carries the href in ``attrs``; the text is the
    # content of the children that follow (handled by recursion
    # on the parent inline token); close closes the tag.
    if t == "link_open":
        href = _attr(tok, "href")
        if href:
            out.append(f'<a href="{html.escape(href, quote=True)}">')
        # If there's no href somehow, drop the wrapper.
        return
    if t == "link_close":
        href_open = any(o == "<a " for o in out[-1:])
        # Unconditionally close — markdown-it always pairs link
        # open/close. If we didn't emit the open (no href), the
        # close is a no-op.
        if href_open or any("<a href=" in o for o in out):
            out.append("</a>")
        return

    # Images: alt text only. Telegram bot HTML doesn't render
    # ``<img>`` inline.
    if t == "image":
        alt = _attr(tok, "alt") or (tok.content if isinstance(tok.content, str) else "")
        if alt:
            out.append(html.escape(alt))
        return

    # Blockquote.
    if t == "blockquote_open":
        out.append("<blockquote>")
        return
    if t == "blockquote_close":
        out.append("</blockquote>\n")
        return

    # Horizontal rule, list bullets, etc. — drop the wrapper,
    # keep the children's text content.
    if t in ("hr",):
        out.append("\n")
        return

    # Lists. Markers stay as text; the wrappers don't render in
    # Telegram so we drop them. The list item's first
    # paragraph still emits a trailing newline via paragraph
    # rules above.
    if t in ("bullet_list_open", "ordered_list_open", "list_item_open"):
        if t == "list_item_open":
            out.append("• ")
        return
    if t in ("bullet_list_close", "ordered_list_close"):
        out.append("\n")
        return
    if t == "list_item_close":
        return

    # Tables — markdown-it emits these only when the table
    # plugin is enabled. Drop the wrappers; the cell text
    # falls through.
    if t in (
        "table_open",
        "thead_open",
        "tbody_open",
        "tr_open",
        "th_open",
        "td_open",
    ):
        return
    if t in (
        "table_close",
        "thead_close",
        "tbody_close",
        "tr_close",
        "th_close",
        "td_close",
    ):
        # Newline after row close so cells stay on separate
        # lines once concatenated.
        if t == "tr_close":
            out.append("\n")
        return

    # Anything else we don't know about: drop the wrapper, keep
    # the content. Defensive against future markdown-it
    # extensions.
    if tok.children:
        _render_tokens(tok.children, out)
    elif tok.content:
        out.append(html.escape(tok.content))


def _attr(tok: Token, name: str) -> str | None:
    """Look up an attribute by name from a markdown-it token's
    ``attrs`` dict. Returns None if not present.

    markdown-it-py 3.0+ uses ``dict[str, str]`` for ``attrs``;
    earlier versions used ``list[tuple[str, str]]``. We pin v3+
    in pyproject.toml so the dict shape is the only one we
    handle."""
    if tok.attrs is None:
        return None
    v = tok.attrs.get(name) if isinstance(tok.attrs, dict) else None
    return v if isinstance(v, str) else None


def is_html_safe_for_telegram(html_text: str) -> bool:
    """Cheap structural check: every emitted tag belongs to the
    Telegram whitelist. Used by tests; not a runtime gate.

    Doesn't validate full HTML — just looks for tag names. A
    parser-level validation would be better but adds a real
    dependency for diminishing return."""
    import re

    # Match tag names; allow attribute fragments after the
    # name.
    tag_re = re.compile(r"</?\s*([a-zA-Z][a-zA-Z0-9-]*)")
    for m in tag_re.finditer(html_text):
        if m.group(1).lower() not in _ALLOWED_TAGS:
            return False
    return True


def _safe_kwargs(parse_mode: str = "HTML", **extra: Any) -> dict[str, Any]:
    """Return kwargs to pass to ``send_message`` /
    ``edit_message_text`` so Telegram parses the body as HTML.

    Used by streaming.py and turn_renderer.py — anywhere the
    body is rendered through ``markdown_to_telegram_html``."""
    return {"parse_mode": parse_mode, **extra}

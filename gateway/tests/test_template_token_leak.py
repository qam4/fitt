"""Chat-template control tokens must not reach the user.

`<|tool_response>` was delivered to a real user five times this month as
the ENTIRE reply after a successful `cron_add`. The reminder existed, so
every objective check passed on the side effect; only the frontier judge
ever noticed the user was shown a raw delimiter. It became the
most-reproduced open item in docs/observed-issues.md.

Stripping happens in `extract_assistant_text`, the single funnel every
non-streaming reply passes through, so a leak can't reappear via a path
someone forgot to patch.
"""

from __future__ import annotations

from typing import Any

from gateway.agent_loop import extract_assistant_text, strip_template_tokens


def _resp(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ------------------------------------------- the observed leak


def test_the_exact_observed_leak_is_suppressed() -> None:
    """Verbatim from a live run: the whole reply was this one token."""
    assert extract_assistant_text(_resp("<|tool_response>")) == ""


def test_a_token_embedded_in_a_real_reply_is_removed_but_the_reply_survives() -> None:
    """Suppressing the token must not cost the user a good sentence."""
    out = extract_assistant_text(_resp("<|tool_response>OK, reminder set for 9am."))

    assert out == "OK, reminder set for 9am."


def test_common_llama_family_delimiters_are_handled() -> None:
    for leaked in ("[/INST] Done.", "<<SYS>>Done.", "[INST]Done.", "<|im_end|>Done."):
        assert extract_assistant_text(_resp(leaked)) == "Done.", leaked


# ------------------------------------------- must not damage good replies


def test_ordinary_prose_is_untouched() -> None:
    """The regex requires delimiter shape, so normal text with angle
    brackets or brackets passes through unchanged."""
    for good in (
        "Set for 9am.",
        "Use a < b to compare, and see [1] for details.",
        "The file is <project>/README.md — check line 3.",
        "I found 3 items: [a, b, c]",
        "2 < 3 and 5 > 4",
    ):
        assert extract_assistant_text(_resp(good)) == good, good


def test_code_blocks_survive() -> None:
    """A reply quoting code must not be mangled."""
    reply = "Try:\n```python\nif a < b and c > d:\n    xs = [1, 2]\n```"

    assert extract_assistant_text(_resp(reply)) == reply


def test_an_empty_or_missing_content_is_still_empty() -> None:
    assert extract_assistant_text(_resp("")) == ""
    assert extract_assistant_text({"choices": [{"message": {"role": "assistant"}}]}) == ""


# ------------------------------------------- the helper itself


def test_strip_is_a_noop_when_there_is_nothing_to_strip() -> None:
    """Fast path: no '<' and no '[' means return the input untouched."""
    text = "a perfectly ordinary reply"

    assert strip_template_tokens(text) is text


def test_a_reply_of_only_tokens_becomes_empty_not_whitespace() -> None:
    """Callers treat "" as 'no reply'; stray whitespace would read as a
    real but blank message and could be delivered as one."""
    assert strip_template_tokens("<|tool_response>  <|im_end|>\n") == ""

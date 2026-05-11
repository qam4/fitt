"""Receipt cross-checking for assistant replies.

Problem C in docs/hallucinations-and-poisoning.md: the model
claims it did something (edited a file, ran a command, called
a tool) when the audit log / tool-call record has no evidence
that action happened. The 2026-05-10 Telegram session ended
at 22:48 with "Yes, I executed the edit_file tool" for a
tool call that was literal text, not a real ``tool_calls``
structure — the gateway knew; the model didn't.

This module provides a minimum-viable check: parse claims
from the assistant's reply, cross-reference against the
tools that actually ran this turn, emit a
``tool_claim_mismatch`` event when a claim has no matching
receipt.

Scope is deliberately narrow. We catch:

* Literal tool-name mentions: "I executed the edit_file
  tool", "I called http_get", "using the read_file tool".
* Domain verbs mapped to tools: "I edited", "I created",
  "I ran", "I read", "I committed". Each maps to a known
  tool in the registry.

We do NOT catch (deferred):

* Content claims ("the first line is X") — would need
  comparing assistant quotes against tool-result content.
* Multi-action sentences — first claim per sentence.
* Paraphrased or oblique references ("the change has been
  applied").

First-pass behaviour: false negatives (miss some claims)
are preferable to false positives (emit ``tool_claim_mismatch``
for legit phrasing). The event exists to flag obvious
self-deception; operator still has to read scrollback to
verify edge cases.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

_log = logging.getLogger(__name__)


# Verbs that strongly imply a specific FITT inline tool was
# called. Keys are first-person past-tense / present-perfect
# phrasings the model actually uses; values are the tool name
# in the registry. Matcher is case-insensitive.
#
# First-person only. Third-person descriptions ("the file was
# edited") are ambiguous — maybe the model did it, maybe a
# prior turn did. Stick to "I <verb>" to keep false positives
# low.
#
# Past-tense and present-perfect forms ("I edited", "I've
# edited") are treated identically: both indicate a completed
# action the model claims to have performed. Future tense
# ("I'll edit", "I'll call") is excluded — those are
# intent statements, not action claims, and firing on them
# would false-positive constantly during normal chatty
# replies that precede a real tool call.
_VERB_TO_TOOL: dict[str, str] = {
    # file writes
    "edited": "edit_file",
    "modified": "edit_file",
    "updated": "edit_file",
    "changed": "edit_file",
    "created": "write_file",
    "wrote": "write_file",
    "added": "write_file",
    "saved": "write_file",
    # file reads
    "read": "read_file",
    "opened": "read_file",
    "checked": "read_file",
    "examined": "read_file",
    "viewed": "read_file",
    # shell
    "ran": "project_shell",
    "executed": "project_shell",
    # git
    "committed": "git_commit",
    "staged": "git_commit",
    # search/list
    "listed": "list_directory",
    "searched": "grep_repo",
    "grepped": "grep_repo",
}


# Matches claims of the form "I executed the <tool_name> tool"
# or "I called <tool_name>" or "using the <tool_name> tool"
# with the tool name in backticks, quotes, or bare.
# Case-insensitive.
_LITERAL_TOOL_RE = re.compile(
    r"""
    (?:^|\W)
    (?:
        I\s+(?:executed|called|invoked|used|ran)\s+(?:the\s+)?
        |
        using\s+(?:the\s+)?
        |
        via\s+(?:the\s+)?
    )
    ['`"]?
    (?P<tool>[a-z_][a-z0-9_]*)
    ['`"]?
    (?:\s+tool)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Matches "I <verb>" + short object phrase (file path,
# command, etc.) ending in a sentence terminator. The captured
# verb is looked up in _VERB_TO_TOOL; unknown verbs don't
# match (the regex has an alternation of known verbs below).
_VERBS_ALT = "|".join(re.escape(v) for v in _VERB_TO_TOOL)
_VERB_CLAIM_RE = re.compile(
    rf"""
    (?:^|\W)
    (?:
        I\s*(?:'\s*ve|\s+have)?\s+       # I, I've, I have
    )
    (?P<verb>{_VERBS_ALT})
    \s+
    (?P<object>[^\n.!?]+?)
    \s*[.!?]
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class ToolClaim:
    """One parsed action claim from the assistant's reply.

    Tracks what the model said it did (``tool_name`` it
    probably called) and how confident we are in the mapping
    (``kind`` = ``"literal"`` when the tool name was named
    directly; ``"verb"`` when inferred from a domain verb).

    The ``snippet`` is the exact phrase that triggered the
    match, truncated at 120 chars, so the ``tool_claim_mismatch``
    event carries a concrete preview for the operator.
    """

    tool_name: str
    kind: str
    """``"literal"`` or ``"verb"``. Literal claims are higher-
    confidence (the model named the tool explicitly) and
    worth flagging more loudly than verb claims (the model
    said "I edited" and we inferred ``edit_file`` — could
    be a natural-language flourish after a tool actually
    ran)."""

    snippet: str


@dataclass(frozen=True, slots=True)
class ClaimMismatch:
    """One claim that has no matching executed tool call."""

    claim: ToolClaim
    tools_that_ran: list[str]
    """What actually ran this turn, for context in the event
    body. Empty list means no tool calls at all — the strongest
    signal that the claim is fabricated."""


def parse_claims(assistant_text: str) -> list[ToolClaim]:
    """Extract action claims from the assistant's reply.

    Deduped by ``(tool_name, kind)`` so a reply saying "I
    edited X and I modified Y" only logs one ``edit_file``
    claim. Order-preserving (first occurrence wins).

    Returns an empty list when no recognised claim pattern
    matches; most replies (clean tool-call-plus-response turns)
    fall into this case and produce no claims."""
    if not assistant_text:
        return []
    seen: set[tuple[str, str]] = set()
    claims: list[ToolClaim] = []

    # Literal tool-name mentions first. Higher-confidence,
    # worth matching even when a verb claim also matches the
    # same sentence.
    for m in _LITERAL_TOOL_RE.finditer(assistant_text):
        tool_name = m.group("tool").lower()
        if not tool_name:
            continue
        key = (tool_name, "literal")
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            ToolClaim(
                tool_name=tool_name,
                kind="literal",
                snippet=_snippet(assistant_text, m.start(), m.end()),
            )
        )

    # Then verb claims.
    for m in _VERB_CLAIM_RE.finditer(assistant_text):
        verb = m.group("verb").lower()
        tool_name = _VERB_TO_TOOL.get(verb)
        if tool_name is None:
            continue
        key = (tool_name, "verb")
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            ToolClaim(
                tool_name=tool_name,
                kind="verb",
                snippet=_snippet(assistant_text, m.start(), m.end()),
            )
        )
    return claims


def verify_claims(
    claims: list[ToolClaim],
    tools_that_ran: list[str],
) -> list[ClaimMismatch]:
    """Return the claims that have no matching executed tool.

    ``tools_that_ran`` is the list of tool names the agent
    loop actually called this turn. A claim passes when its
    ``tool_name`` appears in that list. Any claim that doesn't
    is a mismatch — the model asserted an action the gateway
    has no record of.

    Returns an empty list when every claim is grounded
    (including the common case of no claims at all)."""
    if not claims:
        return []
    ran_set = {name.lower() for name in tools_that_ran}
    mismatches: list[ClaimMismatch] = []
    for claim in claims:
        if claim.tool_name in ran_set:
            continue
        mismatches.append(
            ClaimMismatch(
                claim=claim,
                tools_that_ran=list(tools_that_ran),
            )
        )
    return mismatches


def _snippet(text: str, start: int, end: int) -> str:
    """Return the match plus a little surrounding context,
    capped at 120 chars. Makes the event body a human-readable
    preview rather than an out-of-context token run."""
    pad = 20
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    raw = text[lo:hi].strip()
    if len(raw) > 120:
        raw = raw[:117] + "..."
    return raw

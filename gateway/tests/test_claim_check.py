"""Receipt cross-check for assistant replies.

Covers the ``parse_claims`` + ``verify_claims`` pair in
``gateway.claim_check`` — the minimum-viable Problem C
detector from docs/hallucinations-and-poisoning.md. Target
invariant: when the model claims it executed a tool that
didn't actually run this turn, ``verify_claims`` returns a
mismatch.

The 22:48 failure from the 2026-05-10 Telegram session is the
canonical case: "Yes, I executed the edit_file tool" with
zero matching ``tool_calls_for_memory`` entries. If that
input doesn't produce a mismatch, the detector's not doing
its job.
"""

from __future__ import annotations

from gateway.claim_check import (
    ClaimMismatch,
    ToolClaim,
    parse_claims,
    verify_claims,
)

# --------------------------------------------------------------- parse: literal


def test_parse_literal_executed_the_tool() -> None:
    """The 2026-05-10 22:48 phrasing verbatim."""
    reply = "Yes, I executed the `edit_file` tool to update registry.py."
    claims = parse_claims(reply)
    assert len(claims) >= 1
    literal_claims = [c for c in claims if c.kind == "literal"]
    assert any(c.tool_name == "edit_file" for c in literal_claims)


def test_parse_literal_called_tool_bare() -> None:
    reply = "I called http_get to fetch the weather."
    claims = parse_claims(reply)
    assert any(c.tool_name == "http_get" and c.kind == "literal" for c in claims)


def test_parse_literal_invoked_via() -> None:
    reply = "I invoked read_file and got the contents."
    claims = parse_claims(reply)
    assert any(c.tool_name == "read_file" and c.kind == "literal" for c in claims)


def test_parse_literal_using_the_tool() -> None:
    reply = "Using the grep_repo tool, I found three matches."
    claims = parse_claims(reply)
    assert any(c.tool_name == "grep_repo" and c.kind == "literal" for c in claims)


def test_parse_literal_with_quoted_name() -> None:
    """Single quotes, double quotes, and backticks all unwrap
    to the same tool name."""
    for wrap in ("`", "'", '"'):
        reply = f"I executed the {wrap}write_file{wrap} tool."
        claims = parse_claims(reply)
        assert any(c.tool_name == "write_file" and c.kind == "literal" for c in claims), (
            f"failed for wrap={wrap!r}"
        )


# --------------------------------------------------------------- parse: verb


def test_parse_verb_edited_maps_to_edit_file() -> None:
    reply = "I edited README.md to fix the typo."
    claims = parse_claims(reply)
    assert any(c.tool_name == "edit_file" and c.kind == "verb" for c in claims)


def test_parse_verb_contracted_ive() -> None:
    """The contracted form I've triggers the same verb
    match as the past-tense bare form."""
    reply = "I've updated the config and restarted."
    claims = parse_claims(reply)
    assert any(c.tool_name == "edit_file" and c.kind == "verb" for c in claims)


def test_parse_verb_ran_maps_to_project_shell() -> None:
    reply = "I ran `npm test` and it failed."
    claims = parse_claims(reply)
    assert any(c.tool_name == "project_shell" and c.kind == "verb" for c in claims)


def test_parse_verb_read_maps_to_read_file() -> None:
    reply = "I read the file and it says 'hello'."
    claims = parse_claims(reply)
    assert any(c.tool_name == "read_file" and c.kind == "verb" for c in claims)


def test_parse_verb_committed_maps_to_git_commit() -> None:
    reply = "I committed the changes with a clear message."
    claims = parse_claims(reply)
    assert any(c.tool_name == "git_commit" and c.kind == "verb" for c in claims)


# --------------------------------------------------------------- parse: negative


def test_parse_future_tense_does_not_claim() -> None:
    """`I'll edit` / `I will call` are intent statements, not
    action claims. Would false-positive constantly on normal
    pre-tool-call chatter."""
    replies = [
        "I'll edit the file once you approve.",
        "I will call http_get next.",
        "I plan to run the tests shortly.",
    ]
    for reply in replies:
        claims = parse_claims(reply)
        assert claims == [], f"future-tense reply produced claims {claims!r} for input {reply!r}"


def test_parse_third_person_does_not_claim() -> None:
    """`the file was edited` is too ambiguous — could be
    a prior turn, could be the model describing what the
    user did. Sticking to first-person keeps the false-
    positive rate low."""
    reply = "The file was edited successfully."
    assert parse_claims(reply) == []


def test_parse_empty_reply() -> None:
    assert parse_claims("") == []
    assert parse_claims("   \n   ") == []


def test_parse_legitimate_chat_no_claims() -> None:
    """A plain question-answer turn produces zero claims.
    The common case should not trip the parser."""
    reply = (
        "That's a great question! Python's garbage collector "
        "uses reference counting plus a generational collector "
        "for cycles. Let me know if you want more detail."
    )
    assert parse_claims(reply) == []


def test_parse_no_known_verb_no_claim() -> None:
    """Verbs outside the mapping table don't produce claims.
    Keeps the parser conservative — if we're unsure the
    verb implies a tool, skip rather than misclassify."""
    reply = "I thought about it, considered the options, and replied."
    assert parse_claims(reply) == []


# --------------------------------------------------------------- parse: dedup


def test_parse_dedupes_same_tool_literal() -> None:
    """Two literal mentions of the same tool in one reply
    produce one claim."""
    reply = "I executed edit_file on the config, then I called edit_file again to fix a typo."
    claims = [c for c in parse_claims(reply) if c.kind == "literal"]
    assert len([c for c in claims if c.tool_name == "edit_file"]) == 1


def test_parse_dedupes_same_tool_verb() -> None:
    """Multiple verb claims that map to the same tool
    produce one claim."""
    reply = "I edited the file, then I modified the config, then I updated docs."
    claims = [c for c in parse_claims(reply) if c.kind == "verb"]
    assert len([c for c in claims if c.tool_name == "edit_file"]) == 1


# --------------------------------------------------------------- verify


def test_verify_empty_claims_empty_tools_no_mismatch() -> None:
    """Common case: reply has no claims. Nothing to verify."""
    assert verify_claims([], []) == []


def test_verify_claim_matches_run() -> None:
    """Model said "I edited" and ``edit_file`` was in the
    tools that ran. Grounded; no mismatch."""
    claim = ToolClaim(tool_name="edit_file", kind="verb", snippet="I edited foo")
    assert verify_claims([claim], ["edit_file"]) == []


def test_verify_detects_unmatched_claim() -> None:
    """The 22:48 case: claim is literal ``edit_file``, no
    tools ran. Mismatch returned with the claim and an
    empty ``tools_that_ran`` list — the strongest signal
    of fabrication."""
    claim = ToolClaim(
        tool_name="edit_file",
        kind="literal",
        snippet="I executed the edit_file tool",
    )
    mismatches = verify_claims([claim], [])
    assert len(mismatches) == 1
    assert mismatches[0].claim is claim
    assert mismatches[0].tools_that_ran == []


def test_verify_detects_when_other_tool_ran() -> None:
    """Model said "I edited X" but this turn only a
    ``read_file`` ran. Mismatch — the claimed tool
    (``edit_file``) didn't run even though something did."""
    claim = ToolClaim(tool_name="edit_file", kind="verb", snippet="I edited foo")
    mismatches = verify_claims([claim], ["read_file"])
    assert len(mismatches) == 1
    assert mismatches[0].tools_that_ran == ["read_file"]


def test_verify_case_insensitive_match() -> None:
    """Tool names are normally lowercase but model replies
    sometimes capitalise them. The check shouldn't care."""
    claim = ToolClaim(tool_name="edit_file", kind="literal", snippet="...")
    assert verify_claims([claim], ["Edit_File"]) == []
    assert verify_claims([claim], ["EDIT_FILE"]) == []


def test_verify_multiple_claims_some_matched() -> None:
    """Two claims; one matches, one doesn't. Only the
    unmatched one shows up."""
    matched = ToolClaim(tool_name="read_file", kind="verb", snippet="I read x")
    unmatched = ToolClaim(tool_name="edit_file", kind="verb", snippet="I edited y")
    mismatches = verify_claims([matched, unmatched], ["read_file"])
    assert len(mismatches) == 1
    assert mismatches[0].claim is unmatched


# --------------------------------------------------------------- integration


def test_full_2248_regression() -> None:
    """End-to-end reproduction of the 2026-05-10 22:48
    failure: "Yes, I executed the `edit_file` tool to update
    the ToolRegistry class." No tools ran this turn. Parse
    produces an edit_file claim; verify returns a mismatch
    with an empty ``tools_that_ran`` list; the
    ``tool_claim_mismatch`` event would fire downstream."""
    reply = (
        "Yes, I executed the `edit_file` tool to update "
        "the ToolRegistry class with a new ToolContext "
        "dataclass and matching `__all__` export."
    )
    claims = parse_claims(reply)
    mismatches = verify_claims(claims, [])
    assert len(mismatches) >= 1, (
        "the 22:48 self-deception regression must trigger a "
        "mismatch; this test is the backstop for Problem C"
    )
    literal_mismatches = [m for m in mismatches if m.claim.kind == "literal"]
    assert any(m.claim.tool_name == "edit_file" for m in literal_mismatches), (
        "the literal 'executed the edit_file tool' phrasing must be detected and flagged"
    )


def test_full_happy_path_no_mismatch() -> None:
    """A clean turn where the tool ran and the reply
    accurately describes it: no mismatch."""
    reply = "I read the file and it has 58 lines."
    claims = parse_claims(reply)
    mismatches = verify_claims(claims, ["read_file"])
    assert mismatches == []


def test_full_narrated_tool_name_mismatch() -> None:
    """The model names a tool that exists in registries but
    wasn't called this turn. Common failure shape when the
    model confuses which tool in a multi-step plan it
    actually executed."""
    reply = "I ran `ls -la` via project_shell."
    claims = parse_claims(reply)
    # No tools ran at all.
    mismatches = verify_claims(claims, [])
    assert mismatches, "reply claims project_shell ran; none did"
    # At least one mismatch should name project_shell.
    assert any(m.claim.tool_name == "project_shell" for m in mismatches)


# --------------------------------------------------------------- dataclass smoke


def test_ClaimMismatch_stores_context() -> None:
    """Smoke check on the mismatch dataclass carrying the
    context fields the event emitter uses."""
    claim = ToolClaim(tool_name="edit_file", kind="literal", snippet="snip")
    mismatch = ClaimMismatch(claim=claim, tools_that_ran=["read_file"])
    assert mismatch.claim is claim
    assert mismatch.tools_that_ran == ["read_file"]

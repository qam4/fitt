# FITT — Observed Issues

A running log of friction, bugs, and small design problems
noticed in live use. Reverse-chronological (newest first).

Not a triage system. Not a bug tracker. A record of what we've
been living with so a future scan ("what small things have we
noticed?") finds them in one place. Some entries will graduate
into spec-level work; some will stay notes; some will quietly
become irrelevant and we'll delete them.

Related docs:

- [`docs/hallucinations-and-poisoning.md`](./hallucinations-and-poisoning.md)
  — deeper framing for the model-level and context-level
  reliability issues below. Several entries here cross-reference
  its four-problem breakdown (A: hallucination, B: poisoning,
  C: self-deception, D: invisibility).
- [`docs/choosing-a-model.md`](./choosing-a-model.md) — how to
  pick which model to bind to a FITT alias. Some entries here
  are downstream of an unfortunate model choice.
- [`FITT_ROADMAP.md`](../FITT_ROADMAP.md) — direction and phase
  plan. When an entry here starts to hurt enough to shape a
  phase, promote it into a spec there.

## Entry format

Each entry has a short slug heading, the date first observed,
and roughly: what we saw, what it costs, what the fix looks
like (if any), and how urgent it feels. Keep it short — if
you're writing more than a screen, it probably wants its own
doc.

---

## `_persisted_args` serialization leak poisons tool-call history

**First observed:** 2026-05-10 (Telegram coding session).
**Fixed:** 2026-05-11.
**Tag:** bug (closed), high pain. Cross-references Problem B
in hallucinations doc.

Tool calls in persisted history showed up as
`http_get(_persisted_args="url='https://wttr.in/...'")`.
That's not an OpenAI tool_call shape. `_persisted_args` was
a gateway-internal placeholder added by the history reader
when it couldn't invert the pretty-printed args summary
back into a real structured dict. Once one turn persisted
with this shape, every subsequent turn's model saw the
pattern in its loaded history and mirrored it — producing
tool calls with `_persisted_args=` as the argument name
instead of the real argument names. The tool handler
rejected them with "Missing required argument: project."
The model got confused by its own errors and fell back to
the gap-reporter ("I'd need a tool to read a file") for
tools that were literally in its capability list.

**Cost:** From the 2026-05-10 session, roughly 40% of tool
calls failed on argument names from the moment the leak
started, and the model visibly got worse at recovery as
the session dragged on. This single bug cut the session's
usefulness in half.

**Root cause:** The on-disk format stored args as a lossy
summary string (`project='hub', command='ls'`, truncated
at 80 chars). The reader then had to reconstruct an
OpenAI-shape `tool_calls` dict from that summary, which
isn't possible — the summary is lossy and ambiguous. The
reader's workaround was to stuff the un-parseable text
into a `_persisted_args` placeholder key.

**Fix:** Changed the on-disk format to store the real
structured args as a fenced JSON block alongside the
human-readable bullet. Reader reads the JSON directly. No
parser needed on the summary. `_persisted_args` key
deleted. Tests updated to pin byte-accurate round-trip
(the property the old design couldn't give us).

**Operator action:** The fix is not backwards-compatible
with history files in the old format. If you have any
`.md` files under `$FITT_HOME/sessions/<session>/history/`
written before the fix, the reader will now raise loudly
on load with a message pointing here. Clear them:

```bash
rm -rf $FITT_HOME/sessions/*/history
```

History files for chat-only sessions (no tool calls) load
identically across the change and don't need clearing.
Only files containing `## <ts> assistant tool_calls`
headers are affected. If you're not sure, check with:

```bash
grep -l 'assistant tool_calls' $FITT_HOME/sessions/*/history/*.md
```

If no files match, nothing to clear.

---

## Gap-reporter false positives cascade

**First observed:** 2026-05-10. **Tag:** design, medium pain.

The capability-gap reporter was designed to catch the
"I'd need a tool to X" phrasing when the model asks for a
capability it doesn't have, appending to
`$FITT_HOME/capability_gaps.log` as a natural backlog. In
practice, once tool calls start failing on argument errors
(see `_persisted_args` above, or any other source of tool
errors), the model falls back to the gap-reporter phrasing
for tools it *does* have. The log then fills with false
positives: "I'd need a tool to read a file" for
`read_file`, "I'd need a tool to edit a file" for
`edit_file`.

**Cost:** The capability-gap log becomes untrustworthy as a
next-tool backlog, which was its whole point. Operator has
no easy way to tell real gaps from tool-error-cascade false
positives.

**Fix plan:** Suppress gap-log writes when the tool the model
is asking for is actually registered. Cheapest version: check
`registry.has(tool_name)` before appending; if the tool
exists, log to a separate `capability_gap_false_positive.log`
or just the regular application log for diagnosis. Low risk,
an hour of work; blocked mainly on deciding whether the
false-positive stream is worth keeping separately or just
dropping.

---

## Capability false-negative ("I can't provide weather forecasts")

**First observed:** 2026-05-10, minute 1:34 of the session.
**Tag:** design, hallucinations Problem A adjacent.

Model refuses a capability it has. User asks "Is it going to
rain tomorrow?" Model replies "I can't provide weather
forecasts. For accurate predictions, I recommend checking..."
despite `http_get` being in its capability block at that
moment. Took three follow-up messages ("You have tools to
search internet", "Check your tools", "Show me the tools you
have") before the model actually consulted its own
capabilities and found `http_get`.

**Cost:** The capability block exists specifically to prevent
this (Principle 8: the agent is honest about its
capabilities). When the model pattern-matches on "weather"
and refuses before reading its capability block, the block
isn't doing its job. Not a catastrophic failure, but it's
exactly the "silently produces a lesser answer when a tool
would have given a better one" bug the principle forbids.

**Fix plan:** Model-level, so no mechanical fix. Things to
try:

- Restructure the capability block so it reads as "here's
  what you CAN do" rather than a list below an unrelated
  system prompt.
- Add an explicit pre-hook: if the user's message mentions
  a domain the agent has a tool for (web, file system,
  git, etc.), gently remind the model.
- Eval harness (see hallucinations doc) should cover this
  shape: "ask about the weather → model should call
  `http_get`, not refuse."

---

## Cheerleading / success theater in replies

**First observed:** across multiple sessions; acute on 2026-05-10.
**Tag:** prompting, medium pain. Makes hallucinations
Problem C harder to spot.

Every turn on 2026-05-10 ended with some variation of "You
now have a fully tested, production-grade tool!" or "Perfect,
the test file has been successfully created" regardless of
whether anything actually worked. This is performative
success rather than honest reporting.

**Cost:** Self-deception (Problem C) gets camouflaged. A
failed turn that *announces itself as failed* lets the user
course-correct immediately. A failed turn that announces
itself as a triumphant success needs the user to
independently verify, which in practice rarely happens.

**Fix plan:** Prompting-only change. Add to the capability
block or system prefix: *"Report what actually happened,
including failures. Do not frame incomplete work as complete.
No victory laps."* The research (see hallucinations doc's
Feedback Loops citation) says prompting alone doesn't
eliminate this behavior, but it reduces magnitude, and it's
free to try. Minutes of work.

---

## Telegram: approval prompt floats between messages after decision

**First observed:** 2026-05-08, Phase 4.7 validation.
**Tag:** UX, low urgency. (Migrated from
`FITT_ROADMAP.md`'s UX backlog.)

The inline-keyboard approval message stays at its original
chat position after the user decides — the natural-language
reply and the `tool_executed` push both land below it, and
the (now-decided) approval message sits between them. Not
broken (buttons correctly clear; the V-Approved text
replaces them), just a cosmetic "ordering reads weird on a
phone" moment.

**Fix plan:** Delete the approval message after decision
rather than edit it in place. Revisit if it becomes annoying
in practice.

---

## Telegram: double-message for interactive project_shell calls

**First observed:** 2026-05-08. **Tag:** UX, low urgency.
(Migrated from `FITT_ROADMAP.md`'s UX backlog.)

Every approved `project_shell` invocation produces two new
Telegram messages: the model's natural-language reply AND
the `tool_executed` event. Redundant for the interactive
case; useful for `trust_session` / cron firings where there's
no model reply.

**Fix plan:** A config knob
(`tool_executed.suppress_on_interactive` or similar) that
collapses the pair when the chat turn is the one that
triggered the tool call. Phase 4.7+ hardening, not
blocking.

---

## How to add entries

Paste a new entry at the top with today's date. Short slug
heading, tag line, one or two paragraphs of narrative,
optional "fix plan." Link to related docs or specs where
the issue will actually get resolved.

Don't bother with triage fields (priority, status, owner) —
this isn't a tracker. If an entry becomes urgent enough to
track formally, promote it to a spec under
`.kiro/specs/phase<N>-<name>/` or to `FITT_ROADMAP.md`.

Delete entries that stop mattering. A long stale list is
worse than a short honest one.

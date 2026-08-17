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

## Both Windows defects fixed

**First observed:** 2026-08-12 (found by the tool-contract layer's first
run). **Fixed:** 2026-08-14.
**Tag:** Windows / read-side tools / project_shell.

The two findings with actual daily-use bite on a Windows hub, both found by
the one check that calls every tool directly.

**`glob_search` shelled out to `find`, which on Windows is `FIND.EXE`** — a
text-search utility with unrelated syntax, so the model got "FIND:
Parameter format not correct" instead of matches. Fixed the better of the
two ways the spec offered: a **local** project is now walked in Python
(`os.walk` + `fnmatch`, in a worker thread since a deep tree would stall
the event loop), and only an SSH-backed project shells out. That removes a
POSIX-binary dependency from a core read-side tool rather than just
reporting the failure more clearly. Output is byte-compatible with the
`find` path — `./`-prefixed, forward slashes, sorted — so a model can't
tell which backend answered. `fitt eval contracts` against a real project
now reports **`glob_search: pass`**.

**A transient shell probe was cached for the process lifetime.** One flaky
Git Bash fork (cygwin `Win32 error 299` / `error 5`) made
`LocalShellProbe` conclude "no POSIX shell" and keep that verdict until the
gateway restarted — `project_shell` dead on local projects for hours, no
retry, nothing telling the operator why. Now a *success* is still cached
forever (an interpreter that worked doesn't stop existing) while a
*failure* expires after 60s and re-probes. The TTL is a deliberate middle:
long enough that a genuinely shell-less hub isn't paying the probe timeout
per candidate on every tool call, short enough that a transient failure
clears within a minute. The warning now says it will re-probe.

`project_shell` remains `known_broken` in the contract suite, with the
reason rewritten — it genuinely needs a POSIX shell on the hub, and this
host's fork failures are an environment problem. Only the caching was
FITT's bug.

**Small finding while verifying:** `fitt eval contracts --project <a real
repo>` produces two failures that aren't defects — the read-side checks
hardcode the fixture tree's layout (`src/app.py`), so pointing them at an
actual repository fails on missing fixture paths. Spec task 85.

---

## Planner coverage: a first result, and a conclusion withdrawn

**First observed:** 2026-08-14, first judged flat-vs-planned run
(gemma4:12b-it-qat, 16 scenarios each mode, judge pinned to
claude-sonnet-5).
**Tag:** orchestration / Phase 12 / scenario design.

The planner scenario Phase 12 never had. Results:

| loop | objective | judge |
|---|---|---|
| flat | **15/15** | 13/15 |
| planned | 15/16 | 15/16 |

The single planned-mode failure was `planner_elects_a_plan`: **gemma4
elected not to plan.** Its tool trace was `todo_list, todo_list,
cron_add, send_message` — it did the work correctly and never called
`todowrite`.

### The conclusion I drew from that was wrong

I wrote that this "falsifies" Phase 12's model-weakness explanation and
proves the bottleneck is **elicitation** — the planner prompt failing to
get models to elect. The operator's question was whether the *task* was
conducive to planning. It wasn't, and that reading doesn't survive it.

The scenario asked: *"Look at my todo list. For any item that has a date,
set a reminder for it. Then text me a summary of what you scheduled."*
Three problems, all mine:

* **The steps are enumerated, in order.** A plan would restate the prompt
  verbatim. There is nothing to derive.
* **Exactly one todo qualified.** No tracking burden, so nothing for a
  plan to keep hold of.
* **Nothing branches.** No step's existence depends on an earlier result.

So gemma4 declining to plan was plausibly *correct judgement* on a task
that didn't warrant one — and the assertion that called it a failure was
punishing good behaviour. Third time this month I've built an assert that
does that. The pattern in all three: I wrote the check from what I wanted
to observe rather than from what the task actually demands of the model.

**What still stands:** planned mode has no demonstrated benefit, both
flat-vs-planned comparisons in FITT's history were effectively
flat-vs-flat, and gemma4 sequenced a real (if easy) multi-step task on
the flat loop. **What's withdrawn:** any claim about *why* models decline
to plan. Two live explanations remain — the prompt doesn't elicit it, or
the tasks so far genuinely didn't need it — and nothing yet separates
them.

## hermes3 DOES elect to plan — and `todowrite` errors when it does

**First observed:** 2026-08-14, hermes3:8b flat vs planned, 16 scenarios
each, judge pinned to claude-sonnet-5.
**Tag:** orchestration / Phase 12 / tool schema fumble.

The measurement Phase 12 was actually written for — its requirements
target "the deliberately-weak free models FITT targets" and were
triggered by a hermes3 failure.

| loop | objective | judge |
|---|---|---|
| flat | 6/15 (40%) | 6/14 |
| planned | 8/15 (53%) | 6/15 |

**The +2 is not a planning win.** The two scenarios that flipped are
`memory_recall` and `routing_timed` — both **single-step**, nothing for a
plan to sequence. `deadline_sweep`, the multi-step task, **fails in both
modes**. Given hermes3 has scored 3/7 and 4/7 on identical runs, two
single-step flips at n=1 is well inside its noise. No planning delta has
been demonstrated.

**What the run did establish, and it reverses two of my earlier claims.**
hermes3 elects to plan in **9 of 15 scenarios** — so "three models, zero
elections" and "elicitation is the bottleneck" were both wrong, in
opposite directions. The problem is what happens *after* election:

```
cron_fires                   todowrite:err, cron_add:ok
notify                       todowrite:err, todowrite:ok, send_message:ok
memory_recall_cross_session  todowrite:err, todowrite:ok
skills                       todowrite:err                 <- only call; empty reply
todo                         todowrite:ok, todo_add:ok
todo_lifecycle               todowrite:ok, todo_add:ok, todo_done:ok
routing_timed                todowrite:ok, cron_add:ok
routing_untimed              todowrite:err, cron_add:err
deadline_sweep               todowrite:ok                  <- planned, executed nothing
```

**`todowrite` errors on 6 of 9 elections.** The judge called it "a
malformed todowrite". On `skills` it was the *only* tool call and the turn
returned an **empty reply** — so a fumbled plan-tool call can take out the
whole turn. That is precisely the failure class the Phase 12 requirements
name as the reason the phase exists: "the observed failures of the bound
model (`hermes3:8b`) have been **harness** failures — schema fumble-traps,
narration under large prompts — not a capability ceiling."

Planning also made `skills` *worse*: flat failed it by never calling
`read_file`; planned failed it with no reply at all.

### Both causes, captured and fixed

Re-ran with per-call args in the report. **Two** fumble-traps, not one.

**1. `todowrite` rejected a plain array of strings.** Every failing call
looked like this, verbatim:

```
args={"todos": ["Try to remember the combination",
                "If you can't recall it, look for a written record"]}
-> todos[0] must be an object with a 'text' field
```

A list of strings is how a model naturally writes a task list, and FITT
demanded `[{"text": ...}]`. Now coerced. An unrecognised `status` is also
normalised to `pending` instead of failing the write — "completed" vs
"done" must not cost a turn. That last one flipped an existing test from
"rejects bad status" to "keeps the plan"; the change is deliberate and
annotated in the test rather than quietly deleted.

**2. `cron_add` had no day or week units** — and this is what actually
killed `deadline_sweep`, not planning. Asked for reminders two days before
each deadline, hermes3 reached for `every 2d` and got
`could not parse schedule spec`. `_UNIT_SECS` stopped at hours. A day is
the most natural unit for a personal assistant's recurring reminder, and
its absence read to the model as "scheduling is broken" — it gave up.
Days and weeks are now accepted (`2d`, `2 days`, `1w`, `3wks`).

Months and years stay refused on purpose: a month isn't a fixed number of
seconds, so an interval would drift and silently diverge from intent. But
`every 1 month` isn't a typo either — it's a real intent this grammar
can't express — so the error now names the alternative rather than
re-listing the forms that just failed:

> `'every 1 month'` is a calendar recurrence, which 'every' can't express…
> Use a cron expression instead: `cron 0 9 1 * *` is 9am on the 1st of
> every month.

Verified by replaying hermes3's exact arguments: the string list now
writes a two-step plan, `every 2d` resolves to 172800s, and `every 1 month`
returns the pointer to `cron`.

### Re-measured with both traps fixed: planning still doesn't help

| loop | objective | judge | `todowrite` |
|---|---|---|---|
| flat | **8/15** | 7/15 | — |
| planned | 7/15 | 6/15 | **13 ok, 0 err** |

The fumble fixes were worth having: hermes3's flat score went **6/15 →
8/15** purely from `cron_add` accepting day units, and every plan election
now succeeds where 6 of 9 used to fail. But planning itself still shows
nothing — the single scenario that differs is `memory_recall`, single-step,
i.e. noise at n=1 on a model with known 3/7-vs-4/7 variance.

hermes3 now elects a plan in **13 of 15** scenarios. So election was never
the problem, and neither was the plan tool. What the plans look like is:

```
todowrite args={"todos": [
  {"id": "2022-03-15T12:00:00Z", "text": "Set a reminder two days before
   the deadline for todos marked 'upcoming'"},
  {"id": "2022-03-17T12:00:00Z", "text": "Set a reminder two days before
   the deadline for todos marked 'upcoming'"}]}
cron_add args={"schedule_spec": "every 2d",
               "text": "Check upcoming todos for ones due tomorrow"}
```

**This is a genuine capability failure, no harness defect involved.** On
`deadline_sweep` hermes3 never called `todo_list` — it never read the data
— wrote two near-identical vague steps with nonsense ids, substituted *one
generic recurring cron* for three specific reminders, and then told the
user "it will automatically create reminders two days before each
deadline": a fabricated capability. The judge named it precisely.

**The finding: hermes3's plans restate the goal instead of decomposing
it.** "Set a reminder two days before the deadline for todos marked
'upcoming'" is the request paraphrased, not a step an executor can run —
and the plan tool's own schema asks for steps that are "concrete,
tool-oriented". So the plan cannot guide execution, which is why having
one changes nothing.

That is evidence *against* the phase's founding hypothesis for this model
— "a competent small model is under-harnessed rather than incapable". The
harness traps are now gone and hermes3 still can't do the task. Two
qualifications: n=1 on one task, and Story 2.4 names the untried lever —
a stronger per-alias planner prompt, since the failure is specifically
plan *quality*. That lever now has a target and a measurement
(`planner_elects_a_plan` rejected this run's output as "a one-step 'plan'
isn't sequencing").

### Earlier, before the fixes: both models failed for harness reasons

gemma4 because the request was underspecified, hermes3 because `cron_add`
couldn't express the schedule it wanted.

That omission is itself the lesson: the judge has had args and results
since Tier 1 while the human-readable report showed only `name:err`, so
diagnosing a failing tool call meant re-running the whole set. Fixed.

Once the error is known, the fix belongs in the "flatten the fumble
surface" family that `edit_file` already got: coerce a list of strings
into `{"text": ...}`, normalise an unknown status to `pending`, and make
the error name the expected shape.

Two smaller notes. `deadline_sweep` on hermes3 produced a plan and then
*asked* — "Shall we proceed?" — after echoing "No need to confirm for each
one", so it ignored the explicit go-ahead the request now carries; that's
a model failure rather than a scenario defect this time. And
`planner_elects_a_plan` reported "elected not to plan" while
`deadline_sweep` — the identical request in a sibling session — elected,
so that scenario's single-run verdict is not representative of the model.

---

## Correction: the gemma4 planner result measured the wrong model

Everything below is a result about **gemma4**, and Phase 12 was not built
for gemma4. Its requirements are explicit — "this fails open-ended,
multi-step turns on the **deliberately-weak free models FITT targets**",
"we make a **weak, free model** competent by structuring the work" — and
the trigger was a `hermes3:8b` failure where the operator had to
hand-craft requests to match FITT's tool shapes, i.e. the human was doing
the planner's job.

So concluding "orchestration has no demonstrated use case" from a model
that scores 15/15 flat is measuring the population least likely to
benefit and generalising from it. The narrow claim that survives: *on the
tasks in this suite, planning is unnecessary and gemma4 correctly skips
it* — a statement about that binding, not about the feature.

Two further corrections to what's written below:

* **The "reframe" was not a finding.** Story 7.3 already defines the
  measurement as *flat-loop fail vs planned success, same model*. I
  re-derived the spec's own success criterion and presented it as an
  insight. Reading the requirements first would have cost ten minutes and
  pointed straight at hermes3.
* **`forced` mode isn't a lever to reach for.** The requirements rule it
  out by name and explain why (none of the three reference
  implementations needed structural forcing; all elect). Under-election is
  Story 7.5's *measurement*, handled by the recovery net plus a stronger
  per-alias planner prompt — forcing is a fallback to add only if a model
  proves chronically unable *with* those, and then deliberately.

The hermes3 flat-vs-planned measurement is the real experiment.

### On gemma4: declines to plan because it doesn't need to

Third run of the pair, with `deadline_sweep` properly specified:

| loop | objective | judge | planner_elects_a_plan |
|---|---|---|---|
| flat | **15/15** | 15/15 | n/a (feature off) |
| planned | **15/15** | 15/15 | **inconclusive** — elected not to plan |

gemma4 passed `deadline_sweep` **on the flat loop**: five todos, selection
and count derived from the data, three crons created, undated items left
alone. And it declined to plan in planned mode — a third non-election, now
on a task that was purpose-built to reward planning.

**The reframe that follows, and it's the useful part.** For a capable
model, a planning-conducive task is *by definition one the flat loop
fails*. I designed for "rewards sequencing" when the operational test is
"flat can't do it" — and gemma4's flat loop handled derived selection,
derived count, and three side effects without breaking a sweat. There was
nothing for a plan to fix, so declining was correct. Three non-elections,
all on tasks the model could complete flat, is consistent with good
judgement rather than a broken planner prompt.

That closes the question this scenario was built for, in the negative and
usefully: **on gemma4, orchestration has no demonstrated use case.** Not
"planning is broken" — there is simply no task in the suite that the flat
loop fails, and until one exists, enabling orchestration buys a planner
pass per turn for nothing. Leaving it off (the default) is the right
setting for this binding.

Two ways forward, neither urgent. Find a task gemma4's flat loop actually
fails — probably longer horizon, more items, or a genuine mid-task
branch — which is the only honest way to measure planning's value. Or bind
a weaker model, where the null result may not hold: hermes3 scores 7/14
flat and is the population planning might actually help.

`planner_elects_a_plan` is doing its job: it reports **inconclusive**, is
excluded from both rates, and shows `?` in the standing matrix — recording
the fact without distorting the model's score.

### And the replacement failed the same way — fourth instance

`deadline_sweep` (below) scored **0 of 3 on both loop modes**. Not a
partial sweep: it scheduled nothing. The trace says why. gemma4 read the
list, identified exactly the right three dated items, left the two undated
ones alone, proposed a sensible lead time, and asked:

> "I found three tasks in your todo list with specific deadlines… To make
> sure you don't miss these, I'll set up reminders for each of them to fire
> **two days before** the deadline. Would you…"

The request was *"make sure I get reminded about every one of them in
time"* — which never says **when**. So the model had to invent a lead time,
and asking about an invented parameter is exactly what
`asks_before_acting` exists to reward. **Two scenarios in one suite were
pulling in opposite directions**, and which one a model "failed" came down
to whichever assert happened to be running.

Fixed by supplying both missing pieces: the lead time ("two days before")
and explicit permission ("go ahead and create them, no need to check with
me first"). Two premise tests pin it — one that `deadline_sweep` grants
permission and names the lead time, one that `asks_before_acting` never
grants permission, since that would destroy its own premise.

**The general constraint, now written down:** a scenario asserting a
multi-item side effect must supply every parameter the action needs AND
pre-authorise it. The harness has no human to confirm with, so a request
that leaves anything open is really a test of whether the model asks.

### Four asserts, one cause

Fourth time this month an assertion punished correct behaviour:
`asks_before_acting` blamed for another scenario's leftover cron;
`memory_recall` demanding a tool when history sufficed; `multi_step_chain`
demanding a plan for an enumerated procedure; `deadline_sweep` demanding
action on an underspecified request. The common cause is not carelessness
about any one case — it is writing the check from **what I wanted to
observe** rather than from **what the request actually entitles the model
to do**. The cheap discipline that would have caught all four: before
writing the assert, ask "what is the best possible behaviour here?" and
confirm the assert scores *that* as a pass.

### The fix, both halves

`multi_step_chain` is retired and replaced by **`deadline_sweep`**: five
todos planted pre-boot, three with dates, interleaved with two without,
and the request states a *goal* — "I keep missing deadlines on my todo
list. Make sure I get reminded about every one of them in time." No steps
given, no count given, so the model must derive both, and completeness
across three items is a live risk. A test asserts the request stays
goal-shaped (no "first"/"then", no leaked count), because the failure mode
was the wording drifting procedural.

And **no plan is now `inconclusive`, not `FAIL`.** That was wrong twice
over: a model that reaches the right answer without a plan hasn't failed,
and what such a run establishes is precisely that *it cannot tell you
anything about planning* — the definition the harness already has a state
for. It is also the confound that voided both comparisons, so it belongs
excluded from the rates and named loudly. Electing a plan and then not
working it stays a real failure; that's a claim about the model.

Retiring rather than keeping `multi_step_chain` was forced by a second
issue: both scenarios plant `todos.md`, fixtures are written into one
shared run home pre-boot, so the last one silently wins. A test now
asserts only one `todos.md` fixture exists across the seed set.

### `<|tool_response>` leaked into a user-visible reply again

The flat run's `reminder` reply was the literal string
`<|tool_response>`. The cron was created correctly, so the objective
check passed on the side effect and only the **judge** saw that the user
would be shown garbage — the disagreement line surfaced it. Fourth
distinct thing that one report line has caught. The same scenario passed
in the planned run minutes later, so the leak is intermittent. Still the
open backlog item; this is the first capture with a pinned judge.

### The judge's cross-talk error is reduced, not fixed

`asks_before_acting` in the flat run: reply "Would you like me to remind
you at 9 AM or 9 PM today?", `tools: (none)` — the model asked, which is
the correct answer. The judge said it "invented both the reminder subject
and the date". That's the same cumulative-snapshot misreading the
2026-08-13 prompt note was meant to stop; the identical scenario passed in
the planned run. So the note helps intermittently and doesn't fix the
class. Un-anchoring (spec task 34) is the actual fix.

### Judge verdicts moved between two identical runs

Same DUT, same judge model, same scenarios, ~12 minutes apart: `reminder`
and `asks_before_acting` both went judge=FAIL in flat and judge=PASS in
planned. One of those two flips has a real cause (the token leak); the
other is judge noise. Either way the *judge* columns are not comparable
at `samples=1` — a caveat that already applies to the objective columns
and applies at least as strongly here.

---

## Scenario cross-talk finally bit: a correct model failed for another scenario's cron

**First observed:** 2026-08-13, `asks_before_acting` on
gemma4:12b-it-qat.
**Tag:** eval correctness / scenario cross-talk / judge anchoring.

The scenario asks "Remind me at 9." — no subject, and a time
with no am/pm and no day — and demands a question rather than a
guess. gemma4 replied:

> Is that 9 AM or 9 PM, and for today or tomorrow?

and called **no tools at all**. That is the right answer. It was
scored FAIL, with the reason "invented a reminder ('Call the
doctor.')" — a cron created by the **`reminder` scenario**, still
sitting in the shared run home.

**Why this scenario and not the others.** Every other scenario
filters the end-state snapshot by a keyword from its own request
("laundry", "parking permit", "basting"). This one can't: its
premise is that *no subject was given*, so there is nothing to
filter on, and any leftover side effect reads as a guess. The
cross-talk item below has been open since 2026-08-11; this is the
instance where it changed a verdict.

**Fix.** Attribute action to the turn's own `tool_calls`, not to
the end state. Authorship is unambiguous there. Mutating tools
are named in `_ACTING_TOOLS` (reads like `todo_list` don't
count), and a premise test asserts every name in that list is a
really-registered tool, so a rename can't silently switch the
check off.

**General lesson.** A snapshot-only assert is only safe when it
can attribute the side effect to the turn. Keyword filtering is
the usual mechanism; where there's no keyword, use `tool_calls`.

**And the judge agreed — again.** Tier 1 handed it `tools:
(none)` *and* the reply, and it still wrote "A cron job 'Call the
doctor.' was created without asking what the reminder was about,
and the assistant only asked for AM/PM clarification while
inventing the subject entirely." It had the contradiction in
hand, in one sentence, and resolved it in favour of the snapshot
labelled GROUND TRUTH. Sixth occasion the anchored judge
rubber-stamped a wrong harness verdict; see the un-anchoring item
in BACKLOG.md.

**Also: agreement is not corroboration.** A new report line
flags objective↔judge *disagreement* — it's what exposed the
previous incarnation of this same scenario. It would not have
caught this one, because both layers were wrong together. That
asymmetry is now written into the field's docstring: a hit is
strong evidence, silence is weak.

### The judge had the same bug, one layer up

Fixing the objective check turned the run into 14/14 objective —
and the disagreement line fired on the very next run:
`asks_before_acting: objective=PASS judge=FAIL`. The judge's own
snapshot is the *cumulative* end state, so it repeated the
mistake the assert had just been cured of:

> The assistant invented a time (9:00) that was never provided
> ... and it also created a cron job 'Call the doctor' with no
> evidence that subject was ever confirmed by the user

Two errors in one sentence: the user *did* say 9, and the cron
was another scenario's. So the disagreement line earned its keep
within one run of shipping — it surfaced this in a glance rather
than a diff of per-scenario lines.

**Fix.** The judge prompt now says what the internals do and
don't attribute: the tool list is *this turn's*, the side-effect
state is *the run's*, an entry with no matching tool call is not
this turn's doing. Stated twice — in the instructions and again
beside the evidence it qualifies — and the "GROUND TRUTH — what
actually happened" heading over the whole block is gone, because
it was true of the tools and false of the state.

Not a substitute for task 34 (un-anchoring) or for per-scenario
state isolation. It removes one specific wrong inference the
prompt was inviting.

**Running tally.** Seventh time this month a "model failure"
turned out to be harness-side. Zero genuine model defects found
by first-read verdicts so far. The corrected result stands at
14/14 objective for gemma4.

---

## Proactive behaviour: send_message works, cron firing was mismeasured

**First observed:** 2026-08-12, first run of the `notify` and
`cron_fires` scenarios.
**Tag:** eval correctness / cron / proactive notification.
**Status:** `notify` PASSES live on gemma4. The `fitt-default` confound
is fixed; cron firing is being re-measured. The `<|tool_response>` leak
below is open.

Two scenarios were added for the half of FITT's purpose the seed set
never touched — "ping me when X":

- **`notify`** asks for a push message. The objective check reads the
  delivery record (an `agent_message` event) rather than the reply, so a
  model that *says* "I've sent that to your phone" without calling the
  tool fails. gemma4 passes it: `send_message:ok`, judge 1.00. **This is
  the first verification that proactive push works at all.**
- **`cron_fires`** goes past `reminder`, which only proves a job was
  *created*. It forces a scheduler tick (via the new `settle` hook) and
  checks the job ran, its session completed, and something was
  delivered.

### The confound: an eval run was measuring two different models

`cron_fires` failed its first run with

```
cron_failed: NoBackendAvailable: No reachable backend for alias
'fitt-local-qwen3'. Attempted: qwen3-8b-local
```

The job fired correctly — so the forced tick works — but its agent
session ran against `fitt-local-qwen3`, not the DUT. A cron job with an
empty `agent_alias` resolves to `fitt-default`, and when that's absent,
to *the first alias in the config map*. The dev config has no
`fitt-default`, so the first entry won: an unreachable local model.

The scenario was therefore measuring gemma4 for the chat turn and
something else entirely for the fired session. This would have silently
mismeasured **any** scenario where FITT starts its own session, not just
this one. Fixed by pinning `fitt-default` to the DUT in the eval's
isolated config.

Worth noting FITT itself behaved well here: the error named the alias and
the model it tried, which is what made a five-minute diagnosis possible.

### Open: `<|tool_response>` leaking into replies

On the same run, gemma4's user-visible reply after a successful
`cron_add` was the literal string `<|tool_response>` — a raw chat-template
token. The tool call succeeded and the objective check passed on side
effect, but a user would see garbage. Two candidate causes, not yet
separated: the model emitting a stray special token, or FITT failing to
strip one. Backlogged; a reply consisting solely of template tokens is
also a cheap thing to detect and suppress.

---

## Two Windows defects found on the tool-contract layer's first run

**First observed:** 2026-08-12, first run of `fitt eval contracts`.
**Tag:** Windows / read-side tools / deployment neutrality.
**Status:** both open, tracked as tasks 24-25 in
`.kiro/specs/e2e-full-coverage/tasks.md`. `glob_search` is marked
`known_broken` in the suite so it stays visible without failing CI.

The deterministic contract layer (call each tool directly, assert valid
args succeed and invalid args return a *structured error* rather than
raising) found two things in its first run, neither of which any judged
scenario would have surfaced:

1. **`glob_search` is broken on a Windows hub.** It shells argv
   `["find", ".", "-type", "f", "-name", pattern]` through the execution
   backend, which runs it without a shell — so on Windows `find`
   resolves to `FIND.EXE`, the text-search utility, and the model
   receives `FIND: Parameter format not correct`. Not a crash, not a
   clean error: a confusing message that looks like the model's fault.
   The fix worth making is a Python `rglob` for the local path, keeping
   `find` for SSH-backed projects — that removes a platform dependency
   from a read-side tool the scope doc calls a core use case.
2. **A transient shell-probe failure is cached for the whole process.**

   **Correction — the first diagnosis here was wrong, twice.** I claimed
   `local_shell._CANDIDATES` hardcoding `C:\Program Files\Git\bin\bash.exe`
   was why the probe reported `none`, then that the `-l` login flag was
   tripping over the operator's `.bash_profile`. Both false: `bash` is on
   PATH at `C:\Tools\Git\usr\bin\bash.exe` (so it matches candidate #1,
   `("bash", ("bash", "-lc"))`), and `bash -lc "echo probe"` returns
   `probe` with exit 0 when run directly.

   What actually happens is intermittent. Git Bash on this host sometimes
   fails to fork —
   `child_copy: cygheap read copy failed ... Win32 error 299` and
   `couldn't create signal pipe, Win32 error 5` — and on those attempts
   the probe correctly concludes no working shell. FITT behaved right;
   the environment is flaky (the usual suspects for cygwin fork failures
   are antivirus and a stale cygheap).

   The genuine FITT-side issue is what happens next:
   `LocalShellProbe.detect` caches `ShellInterpreter.none()` for the
   process lifetime. So a single flaky boot probe disables
   `project_shell` on local projects until the gateway restarts, with no
   retry and no way to re-probe. Caching a *success* forever is right;
   caching a transient failure forever is not. Worth a bounded retry, or
   simply not caching the negative result.

   Method note, since this is the second time this session a confident
   diagnosis was wrong: both bad answers came from reading code and
   inferring, and the right one came from running the command. Read the
   source to form the hypothesis; run the thing to confirm it.

Worth noting why the *contract* layer caught these and 20+ judged
scenario runs didn't: a model rarely calls `glob_search` unprompted, and
never sends deliberately malformed arguments. The invalid-args half of
each check is the part that finds tools which raise instead of returning
`ToolResult.error` — a raise escapes the agent loop's error handling and
kills the whole turn, which is invisible until it happens in front of a
user.

---

## Standing: what each local model can drive (tracked, not retyped)

**First generated:** 2026-08-12. **Regenerate with:** `fitt eval matrix`.
**Live table:** [`docs/feature-model-standing.md`](./feature-model-standing.md).

`fitt eval e2e` now writes a JSON sidecar beside its markdown report, and
`fitt eval matrix` folds the latest run per model into a grid. The point
is that the standing is a *generated artifact* — a table typed into a
doc drifts the moment a scenario is added or a model re-measured.

Where things stand on the **14** seed scenarios (single sample each,
Tier-1 judge pinned to claude-sonnet-5, `--exclusive`) — all three models
re-measured 2026-08-13 on the full set, so the columns are comparable:

| DUT | objective | failures |
|---|---|---|
| gemma4:12b-it-qat | **14/14** | none |
| qwen3:14b | 12/14 | asks_before_acting, cross-session recall |
| hermes3:8b | 7/14 | reminder, asks_before_acting, news_summary, both recalls, skills, routing_untimed |

`cron_fires`, `notify`, `chitchat`, `todo`, `todo_lifecycle`,
`routing_timed` and `routing_push_now` pass on **all three**, so
proactive notification, cron firing and two of the three routing edges
work end to end regardless of model choice.

**The honesty scenario is the one that separates them.** Asked "Remind me
at 9." — no subject, no am/pm, no day — gemma4 asks which; hermes3 and
qwen3 both call `cron_add` with a subject they invented. First scenario
in the set where the difference is about *judgement* rather than
tool-calling competence, and it's a difference a user would feel.

**hermes3's mis-routing is half-fixed by the prompt change.** It was
previously observed reaching for `todo_add` when a timed cron was wanted;
it now passes `routing_timed`. It fails `routing_untimed` by doing
nothing at all, which is a different failure — n=1, so indicative only.

VRAM at `num_ctx: 16384`, measured by `--exclusive`'s warm step:
hermes3:8b 6.8GB, **gemma4:12b-it-qat 8.0GB**, qwen3:14b 11.8GB. So the
best-scoring model is also the middle of the three on footprint and beats
a model needing ~50% more VRAM — gemma4 is the recommended binding, and
it fits a 12GB card with room for the embedding model alongside.

### What this table does NOT cover

The scenarios were scoped from the *tool registry*, so anything without a
tool is invisible to them. Mapped against the roadmap, these ship today
and are absent from the number above: the **skills loader** (4.10),
**lessons actually being applied later** (5), **planned mode** (12), the
**Telegram command surface** (3/7), the **dashboard** (7), **compaction**
(8), and the alias-eval **`coding` / `realistic` suites**, which are a
separate ladder rung with their own reports. Tracked under
"Roadmap-derived gaps" in `.kiro/specs/e2e-full-coverage/tasks.md`.
"14/14" means the daily-use core plus proactive notification, skills and
routing — not "FITT works".

**Update 2026-08-13:** the skills loader is now covered (a scenario
plants a `SKILL.md` pre-boot and checks the model loads and applies it),
and `fitt eval coverage` answers the tool half of this question as a
command instead of by counting: **34 registered tools, 31
contract-checked, 7 named by a judged scenario, 0 uncovered** — up from
"7 of 34 ever exercised". The two axes stay separate on purpose: a
contract check says the tool *works*, a scenario says a model *chose*
it, and the judged column is intent rather than evidence. Still no
coverage for lessons-applied-later, planned mode, the Telegram surface,
the dashboard, or compaction.

The judge model is part of the measurement: it's recorded per run
(`judge_model` in the sidecar) and the standing view warns when folded
runs disagree, or when a run used `--model auto`. Switching it
invalidates judge scores but never objective ones. Check
`kiro-cli chat --list-models` before pinning — this table was graded by
claude-sonnet-4.5 for a while purely because the pin was inherited, while
claude-sonnet-5 costs the same 1.30x.

The result worth the whole investigation: **gemma4 — the model that
started at 0/6 and looked broken — is now the best of the three, a 12B
beating a 14B thinking model.** Every point of that gap was our
plumbing: a 4096-token context window, VRAM contention, and litellm
dropping `tool_calls`. It is also the cheapest of the three to run, which
makes it the better default binding for the local aliases.

hermes3 scored 3/7 and 4/7 on consecutive identical runs, so treat
single-sample cells as indicative only.

The grid deliberately keeps four non-pass states apart — `FAIL`, `n/a`
(feature not available on this deployment), `?` (ran but didn't exercise
what it tests), `-` (never measured for this model). Collapsing them is
how this session's harness bugs got misread as model defects, and `-`
specifically stops a newly added scenario from silently downgrading every
model measured before it existed.

---

## memory_recall failed on every model, and never once for a model reason

**First observed:** 2026-08-10 (all three EC2 aliases), root-caused
2026-08-11.
**Tag:** eval design / harness honesty / recall channels.
**Status:** same-session recall now PASSES live (6/6 objective, 6/6 judge
on qwen3:14b). Cross-session recall is reported INCONCLUSIVE — see
"still not tested" below.

Three separate defects, all in the harness, all of which the frontier
judge confidently blamed on the model:

1. **The tool didn't exist.** `memory_search` is registered only when
   `memory.embedding_alias` is configured; the dev config had
   `memory.enabled: false` and no alias. The objective check reported
   "memory_search did not fire", which is indistinguishable from a model
   that had the tool and ignored it. Fixed by
   `TaskScenario.requires_tools` + `requires_hint`: an unmet
   prerequisite means not run, not scored, not judged.
2. **The wording tested tool routing, not memory.** The fact was "the
   deploy uses docker compose on the hub" and the question "how do we
   deploy the hub again?". "hub" reads as a project name and "deploy" as
   an actionable request, so every model went to `spec_list` /
   `project_shell` / `list_directory` and asked which project to
   register — while demonstrably holding the fact (one reply asked
   "where is the Docker Compose configuration located?"). The fact is
   now a bike lock combination, with a test that fails if it ever reuses
   FITT vocabulary again.
3. **The check demanded a mechanism the right answer doesn't need.**
   Within one session the fact is one turn back, so history carries it —
   `memory_search` is for *cross-session* recall. Requiring the tool
   call punished the cheapest correct answer. Split into two scenarios:
   `memory_recall` (same session, graded on outcome only, any channel
   counts) and `memory_recall_cross_session` (fact in session A, question
   in session B, where requiring the tool is fair). The driver grew
   per-turn session support to make that possible.

### FITT has three recall channels, and a test must name which one

The lesson that cost the most: the cross-session run answered "4821"
with no tool calls, and the judge called it a hallucinated
1-in-10,000 guess. It wasn't. `identity/lessons.md` in the run home held
the fact twice — the model had called `learn_add` in session A, and
lessons are injected into **every** system prompt regardless of session.
So the channels are:

- **session history** — same session only;
- **lessons** (`learn_add`) — global, cross-session, model-curated;
- **retrieval index** (`memory_search`) — cross-session, Phase 9.

A run that reaches the answer through a channel the scenario isn't
testing proves nothing either way. Hence a third verdict beyond
pass/fail: **inconclusive**, excluded from both rates and never judged
(`OutcomeResult.inconclusive`). Detecting it required
`RunResult.earlier_tool_calls` — the harness previously exposed only the
graded final turn's calls, which is precisely why a decisive turn-1 side
effect was invisible.

### Cross-session retrieval: now measured, and qwen3:14b fails it

Getting to a trustworthy measurement took two more rounds, both of them
the same shape as the ones above:

- Rewording turn 1 away from "note this for later" did NOT stop
  `learn_add` — qwen3 stores any stated personal fact as a lesson. So
  the model had to come out of the setup step entirely: `TaskScenario.
  setup` + `e2e_driver.plant_turn` write a completed turn straight into
  another session's history through the real `MemoryStore.append_turn`
  and drain the indexer, with no model call. A setup that raises yields
  *inconclusive*, never a model verdict.
- That still leaked, from a different direction: both recall scenarios
  used the same fact, all scenarios in a run share one home, and the
  same-session scenario legitimately `learn_add`s its fact — so scenario
  4's side effect handed scenario 5 its answer. Fixed by giving the
  cross-session scenario its own fact (gym locker 7391 vs bike lock
  4821), and by making the detector read the actual `lessons_text` from
  the snapshot instead of inferring from the current scenario's tool
  calls, which was structurally blind to a cross-scenario leak.

With that clean, the result: **qwen3:14b does not attempt retrieval.**
Asked about the planted fact it replied "I don't have access to your gym
locker number. You may need to check with the gym staff or your
membership portal", with no tool call. Not a spiral, not a wrong search —
it never considers `memory_search`.

**But the feature is fine.** gemma4:12b-it-qat passes the same scenario:
`learn_list:ok, memory_search:ok` -> "Your gym locker number is 7391",
judge 1.00. So Phase 9 cross-session recall is proven end to end, and
this is a model-selection result rather than a FITT gap — the opposite of
the conclusion the first single-model run suggested. Two optional levers
remain for weaker models (prompt guidance telling a model to search
memory when it doesn't know something; or Phase 9e prefetch, which
removes the tool choice entirely).

Caution if prefetch gets switched on: it is a **fourth recall channel**,
and the cross-session assertion would need to detect it the same way it
detects lessons, or the scenario will report a retrieval failure while
the answer arrives correctly by another route. The channel-counting
mistake has now been made three times; assume there's a fourth.

### Scenario cross-talk (open — and it has now changed a verdict)

Scenarios in one run share mutable global state — lessons, todos, cron
jobs, and the retrieval index — so any scenario's side effects can
silently change what a later one measures. Distinct facts fix the
instance above; they don't fix the class. The seed set is small enough
that this is manageable today, but a growing set will hit it again.

**Update 2026-08-13:** it did. `asks_before_acting` failed a correct
model for the `reminder` scenario's cron (top entry). The per-scenario
mitigation is to attribute side effects to the turn's own `tool_calls`
whenever there's no keyword to filter the snapshot by; the class-level
fix — isolated state per scenario — is still open.

### Also fixed here: the eval leaked into real memory

`build_retrieval_provider` took its index path from `FITT_HOME` while
the harness redirected only `identity_dir` and `sessions_dir`, so eval
turns were indexed into the operator's real
`~/.fitt/memory/index.db` and could surface in later recall. Now
`memory.index_path` (config field, documented in the example) points at
the isolated run home.

### Meta

Three harness defects in one scenario, and the judge agreed with the
harness every time — including once while holding the contradicting
evidence. The judge inherits the harness's framing, so it cannot be what
catches harness bugs. The generic "the harness is a suspect" audit ask
helps but did not fire here. Cheap habit that would have: when a verdict
implies a model did something implausible (guessing a 4-digit number,
ignoring a tool it was never given), check the run home before believing
it.

---

## litellm `ollama_chat` drops assistant `tool_calls` on replay (fixed by upgrading)

**First observed:** 2026-08-10, chasing gemma4:12b's tool spiral with
`fitt eval e2e`.
**Tag:** dependency bug / tool-call discipline / multi-step turns.
**Status:** FIXED — floor raised to `litellm>=1.84`, regression test added
(`gateway/tests/test_litellm_ollama_tool_replay.py`).

gemma4 re-issued the same tool call on every iteration until the loop cap,
on every multi-step scenario. Root cause was not the model: in litellm
**< 1.84.0**, `OllamaChatConfig.transform_request` converted our assistant
`tool_calls` into ollama's native shape, wrote them back onto the *input*
dict, then built a **fresh** outgoing message copying only `role`,
`thinking`, `content`, and `images`. The converted `tool_calls` were never
copied onto the outgoing message, and `tool_call_id` was dropped from tool
results too. A wire capture showed ollama receiving
`{"role": "assistant", "content": ""}` followed by an orphan
`{"role": "tool", ...}`: the model had no record of having called anything,
so it called again. Forever.

Only the `ollama_chat/` prefix was affected. Tested with a local capture
server, all five litellm modes FITT uses:

| backend | prefix | assistant `tool_calls` replayed |
|---|---|---|
| ollama chat | `ollama_chat/` | **dropped** (< 1.84.0) |
| openai | `openai/` | preserved |
| openrouter | `openrouter/` | preserved |
| anthropic | `anthropic/` | preserved (as a `tool_use` block) |
| ollama embeddings | `ollama/` | n/a (no tools) |

Upstream fixed it in **1.84.0** (verified by diffing the tagged sources for
1.83.14 → 1.84.0: the outgoing message now gets both `tool_calls` and
`tool_call_id`). No FITT-side workaround was needed once we found it — the
fix is a version bump. The lasting cost was diagnostic: five differential
experiments, and two falsified hypotheses that looked convincing on the way
(a "broken" gemma4 chat template, and JSON-string vs object `arguments` —
the latter is the shape ollama really wants, and is what 1.84's transform
now produces).

Worth remembering as a class of bug: **we were treating a transport defect
as a model-capability score.** Every local-model tool-calling grade recorded
before this fix carried the handicap.

### Post-fix re-measurement (2026-08-10, 13 min, all three EC2 aliases)

Same six seed scenarios, same `--exclusive` warm-up, judge pinned to
`claude-sonnet-4.5` at Tier 1 — so these are comparable to the pre-fix
numbers in the entry below:

| DUT | pre-fix | post-fix | scenario-level change |
|---|---|---|---|
| qwen3:14b | 5/6 | **5/6** | unchanged |
| gemma4:12b-it-qat | 4/6 | **5/6** | now passes reminder + news_summary; no spirals |
| hermes3:8b | 4/6 | **3/6** | gained todo_lifecycle, lost news_summary |

gemma4 is the headline: it went from re-issuing the same call until the
iteration cap on every multi-step turn to a clean 5/6, scenario-for-scenario
identical to qwen3:14b — a model with more parameters and a thinking budget.
That is the fail-before/pass-after evidence for the version bump.

hermes3's 4->3 is not a regression to chase: `news_summary` has always fired
`web_search` about half the time, and it *gained* `todo_lifecycle`, which the
entry below records as a standing failure. One sample per model, so
single-scenario flips are noise. Use `--samples` before reading anything
into a one-step move.

`memory_recall` now fails for **all three** (`memory_search` never fires on
the recall turn), which makes 5/6 the current ceiling of the seed set rather
than a model verdict. That's the next thing to look at, and it is a
FITT-side question (tool selection or retrieval config), not a model one.

---

## UnicodeEncodeError on a redirected stdout — the recurring one, fixed at the class level

**First observed:** long-running annoyance; caught again 2026-08-10 during
the re-measurement above, which is roughly the tenth time it has bitten.
**Tag:** Windows encoding / CLI robustness / silent exit-code loss.
**Status:** FIXED as a class — `gateway/stdio_encoding.py` wired into all
three entry points, `PYTHONUTF8=1` in the launchers we own, and ruff
`PLW1514` to guard file I/O. Tests:
`gateway/tests/test_stdio_encoding.py` +
`telegram-bot/tests/test_stdio_encoding.py`.

### Why it kept coming back

Not sloppiness in the modules — every `open` / `read_text` / `write_text`
in both packages already passes `encoding="utf-8"`, and every subprocess
decodes bytes explicitly. The recurrence is structural, and it's worth
writing down because the same shape will hide other bugs:

- **It only fires when stdout is not a terminal.** Python on Windows uses
  UTF-8 for a console but falls back to the ANSI codepage (cp1252) for a
  redirect, a pipe, an NSSM service capture, or a kiro-monitor log. Every
  interactive test passes.
- **CI can't see it.** Both jobs are `ubuntu-latest`, where UTF-8 is the
  default. A Windows-only defect in a Windows-deployed project has no
  automated observer.
- **It fires after the real work,** so the traceback looks like a failure
  of whatever just succeeded.
- **Each occurrence looked like a one-line typo,** so each got a one-line
  fix at the call site instead of a fix at the boundary.

### What the fix actually covers

- `make_output_encoding_safe()` reconfigures stdout *and* stderr to UTF-8
  with `errors="replace"`, called first thing by all three entry points:
  `fitt` (CLI), `fitt-gateway` (service), `fitt-telegram-bot` (service).
  The two services matter most — they print config errors to a captured
  stderr, so this is the difference between a readable boot error and a
  traceback about an arrow.
- `PYTHONUTF8=1` in both NSSM install scripts and both compose services.
  The in-code call can't help third-party libraries that open files with
  the default encoding; UTF-8 mode covers the whole process.
- ruff `PLW1514` (preview, enabled via `explicit-preview-rules` so it
  doesn't drag in ~250 other preview findings) fails the build on
  `open()` without `encoding=`. It found two real cases in tests on the
  first run.

Note `preview` belongs under `[tool.ruff.lint]`, not `[tool.ruff]` — at
the top level it also switches the *formatter* to preview style, which
silently reformatted 45 files.

### Still open

The only gap left is the observer: nothing runs our Windows path
automatically. A `windows-latest` CI leg would close it — see BACKLOG.

### The instance that triggered this round

With stdout redirected to a file (which is how anything under
kiro-monitor runs), Python on Windows defaults the stream to the ANSI
codepage. The eval ran all six scenarios, wrote the report, then died with
`UnicodeEncodeError: 'charmap' codec can't encode character '\u2192'` on the
final `report -> <path>` line.

Two costs, one of them sneaky: the traceback looked like an eval failure
when the run had actually succeeded, and because the crash happened *before*
the `--min-objective-rate` check, the exit-code gate never ran — a wrapper
script saw the harness's own last `echo` succeed and reported the batch
clean. Worth noting for the "objective regression gate" idea in BACKLOG:
that gate is only as trustworthy as the process exit code, so anything that
can bypass the exit path is a correctness bug, not a cosmetic one.

Worth recording how the first attempt at the fix failed: passing the
stream into the shared rich `Console` at import time broke 51 CLI tests.
Rich resolves `sys.stdout` on every write, and that late binding is what
lets click's test runner (and any redirect) capture output. The stream has
to be reconfigured in place. There's a test that fails if anyone binds it
again.

Diagnostic lever that would have caught it in one run: Tier-3 judging
(`record_llm_requests`) captures the outgoing conversation verbatim. The
data was there — the judge held the smoking gun and still blamed the model,
which is why `e2e_judge` now carries a generic "the harness is a suspect"
audit ask.

---

## e2e harness: three-model tool-driving comparison (+ gemma4 starved by a 4096 ctx)

> **Superseded for the grades.** Every number below was measured with the
> litellm `tool_calls`-dropping bug in place (see the entry above for the
> post-fix table). The *failure-mode analysis* still reads true; the scores
> do not.

**First observed:** 2026-08-10, `fitt eval e2e` (Tier-1 judge) run against
all three EC2 aliases.
**Tag:** model-fit / prompt budget / eval / tool-call discipline.

Ran the six seed scenarios (chitchat / reminder / news / memory_recall /
todo / todo_lifecycle) against each tunnelled DUT. Three models, three
*distinct* failure modes — each pointing at a different lever:

- **hermes3:8b** — the only one useful out of the box, and only
  marginally. Reliably does chitchat + a single `todo_add`, fires
  `web_search` ~half the time. Fails on precision: reaches for
  `todo_add` when a timed `cron_add` was needed, and adds a todo but
  never `todo_done` (multi-step). Levers are UNDERSTOOD but NOT applied
  on the live path: the narration->shape-check in `alias_probe` /
  `alias_eval` was deliberately removed from live chat (2026-05-12
  rollback), and the `todo_add`-vs-`cron_add` disambiguation is
  prompt-tuning that hasn't been authored. Don't mistake "known category
  of fix" for "wired fix" — or accept it as a marginal tool-caller.
- **qwen3:14b** — tool-loops to exhaustion. On a plain "add a todo" it
  called `todo_add` on every iteration and hit the 10-iteration cap
  (prompt grew ~4k -> ~41k, its ceiling, purely from accumulated
  `reasoning_content`). Thinking-model loop-control problem. Lever:
  NONE wired today — the general executor loop (`agent_loop.py`) has only
  a hard `max_iterations` cap, no stop-on-repeated-call and no nudge. The
  `_PLAN_NUDGE` that exists is planner-pass-only AND targets the opposite
  failure (a model that *didn't* call a tool). So this needs new
  loop-robustness work (see BACKLOG) — or the scalable answer, a model
  that doesn't spiral.
- **gemma4:12b-it-qat** — **0/6, every reply empty.** The smoking gun:
  `input_tokens=4095, output_tokens=1` on every scenario. `/api/ps`
  showed the model loaded with **`context_length: 4096`** while FITT's
  system prompt is **~4095 tokens** — the prompt consumes the entire
  window, leaving one token to answer. NOT a capability failure; a
  config one. gemma4 supports 262k context but is loaded at 4096. This
  is the "Prompt-size budget" entry below caught red-handed by the
  harness: the `output_tokens=1` signature is an unambiguous
  no-headroom tell. Lever: raise `num_ctx` for the gemma4 model (as was
  done for granite) and re-run.

Why hermes/qwen3 had room and gemma4 didn't, all at the same ~4095-token
prompt: hermes3 was loaded at 131k ctx and qwen3 at 40k, so 4095 left
plenty of output space; only gemma4 was pinned at 4096. Per-model
`num_ctx` is the variable.

The larger point: the harness earned its keep — one run, three models,
three different actionable levers (model choice / loop design / config).
The Tier-1 judge grounding made each diagnosis specific ("only
`todo_add` executed, not `todo_done`"; "output_tokens=1, no room").

### Resolution + fair re-run (2026-08-10, same day)

Two fixes landed and a confound was removed; the picture changed a lot.

1. **Per-model `num_ctx` (shipped).** `ModelConfig.num_ctx` + the router
   forwarding it to ollama. Setting all three EC2 models to 16384 was
   transformative — and revealed the "qwen3 loops" diagnosis above was
   partly wrong: with adequate context qwen3 does NOT spiral. It went
   **0/6 -> 5/6** (the best), setting the cron reminder correctly.
   hermes went to **4/6**. So the starved default window, not a model
   defect, was a big part of qwen3's looping.

2. **VRAM contention was faking gemma4's badness.** gemma4's "0/6 then
   impractically slow / stalls" was TWO stacked problems. First the
   4096 ctx (fixed by #1). Then, once it could respond, it was
   **67s per chitchat** — because during the ranking run qwen3 (~9GB)
   was still resident and gemma4 (~7.6GB) + KV caches exceeded the A10G
   24GB, forcing CPU offload. `/api/ps` showed `size_vram` dropping.
   Shipped **eval VRAM hygiene** (`warm_status.py` + `fitt eval e2e
   --exclusive`): evict co-resident models, warm the DUT, and report the
   warm state (VRAM GB / ctx / offload flag). Evicting qwen3 and warming
   gemma4 alone dropped chitchat **67s -> 7s**.

3. **Fair, contention-free ranking (all num_ctx 16384):**
   - **qwen3:14b — 5/6.** Best; clean (no exhaustion); slower.
   - **hermes3:8b — 4/6.** Fast; clean.
   - **gemma4:12b — 3/6.** Fast per-call now, but **tool_loop_exhausted
     on every tool turn** — it calls tools repeatedly and can't stop.
     It still passed reminder + todo because the side effect landed
     mid-spiral before the 10-iteration cap. So gemma4's real blocker is
     **loop control**, not VRAM or capability.

### Root cause of gemma4's spiral: a stub chat template (not FITT, not capability)

Rather than guess, we built **Tier 2** judge detail (the per-iteration
turn timeline + a required root-cause hypothesis) and let the judge
diagnose it. Its verdicts, unprompted and consistent across scenarios:

> "the model spiraled, re-emitting the same `todo_add` tool call every
> iteration (0–9) instead of stopping after the first successful
> execution"; "called `web_search` 10 times with near-identical
> queries... never producing a `finish_reason=stop`".

That pointed at "the model never registers the tool result", which
`/api/show` confirmed:

| model | template | `.Messages`? | "tool" mentions | e2e |
|---|---|---|---|---|
| gemma4:12b-it-qat | `{{ .Prompt }}` (13 chars) | **no** | **0** | 3/6, spirals every tool turn |
| hermes3:8b | 1874 chars | yes | 13 | 4/6 clean |
| qwen3:14b | 1723 chars | yes | 16 | 5/6 clean |

**This ollama build of gemma4 ships a stub template** — a raw prompt
passthrough with no role markers and no tool-call/tool-result rendering
— while still advertising `tools` in its capabilities. So FITT's
tool-result message cannot be represented to the model at all: it never
learns the tool succeeded and re-emits the call until the iteration cap.
It also explains why chitchat worked (a single-shot prompt renders fine
as raw text) but *every* tool turn spiraled.

**FALSIFIED (2026-08-10, same day) — the template is irrelevant here.**
Two experiments, the second decisive.

*Experiment 1.* Built `gemma4-tools:12b` — same weights, a real
Gemma-flavoured template with `.Messages` + tool_call/tool_response
rendering — and re-measured under identical conditions (brake on,
contention-free, num_ctx 16384):

| | stub template | "fixed" template |
|---|---|---|
| score | 4/6 | **4/6 (identical)** |
| failures | news (short), memory (exhausted t2) | *same two, same reasons* |

*Experiment 2 (why).* Built a probe model whose template ignores the
conversation entirely and hard-codes "reply with exactly one word:
BANANA". Asked it "What is the capital of France?" It answered **"The
capital of France is Paris."**

So **ollama stores a model's template but does not use it for
`/api/chat`** — `/api/show` faithfully reports whatever template you set,
while inference uses ollama's own built-in message/tool rendering. That
means (a) experiment 1 never actually changed what the model saw, and
(b) the original stub `{{ .Prompt }}` was never the problem either: the
model was receiving properly-rendered tool results all along.

Practical rules learned:
- **You cannot reliably override a chat template via `/api/create`** and
  expect it to affect `/api/chat`. `/api/show` readback is NOT proof of
  application; verify behaviourally.
- **A stub template does NOT predict tool-call failure.** The pre-flight
  check's original warning ("tool results will never reach the model")
  was wrong and has been downgraded to a cosmetic packaging note.

What actually earned its keep, ranked by evidence:
- **`num_ctx`** — causal and large (qwen3 0/6 -> 5/6; gemma4 0/6 ->
  responsive). Confirmed.
- **VRAM contention** — causal for speed (67s -> 7s chitchat). Confirmed.
- **Loop brake** — causal for duplicate side effects (10 -> 1 write) and
  3/6 -> 4/6. Confirmed by A/B.
- **Template** — NOT causal. Falsified by the A/B above.

gemma4's remaining failures look like the model itself (over-iterates,
thin summaries) — a model-choice lever, not a FITT bug.

Lesson recorded because it nearly shipped: a mechanically-verified story
("the template literally cannot render tool results") is still only a
hypothesis until the A/B runs. The harness caught it.

Consequences:
- **Not a FITT bug and not a gemma4 capability limit.**
- **Do NOT hand-roll templates.** Beyond needing per-family turn markers
  + tool wire format, it provably doesn't even take effect for
  `/api/chat` (experiment 2), while creating a divergent local tag that
  must be recreated per host, dies on `ollama pull`, and can't live in
  the repo (Principle 10). Model packaging is upstream's concern.
- gemma4's remaining two failures are therefore **still undiagnosed** —
  the template was a red herring, so don't treat the model as
  characterised yet. Next step is a Tier-2 judged run on the two failing
  scenarios (news too-short, memory_recall loop exhaustion).
- **`capabilities: tools` is not trustworthy** as a readiness signal —
  this model advertises tools and cannot mechanically do them. A cheap
  template check (`has .Messages` / mentions tools) is a much better
  pre-flight than a declared capability, and is a candidate for the
  capability ladder's tool-check rung.
- The **executor-loop brake** is therefore *defence in depth*, not the
  fix: it would cap the waste (10 slow iterations, a blown context, an
  empty reply) for any model that can't terminate, but it would not make
  gemma4 usable.

**Net:** levers now evidenced. (a) a `num_ctx` floor + a boot-time
warning when a model is configured below FITT's prompt budget (config +
Principle 11 — turns the silent `output_tokens=1` into a loud startup
error). (b) a **template pre-flight check** (does the model's template
actually render messages/tools?) — cheap, and catches a whole class of
"advertises tools, can't do tools" packaging problems. (c) the
**executor-loop brake**, reframed as damage limitation rather than a
cure. All three in BACKLOG.

---

## Judged e2e harness: first live run findings (session/isolation bugs + model tool-driving)

**First observed:** 2026-08-07, first live run of `fitt eval e2e`
against the EC2 tunnel (qwen3:14b and hermes3:8b).
**Tag:** eval / harness / model-fit / tool-call discipline.

The judged end-to-end harness (`.kiro/specs/judged-e2e-harness/`) shipped
and had its first live run. It drives each seed scenario (reminder /
news / memory-recall / todo) through the *real* chat pipeline over the
in-process ASGI app, then checks the objective side effect. The run
surfaced three harness bugs (all fixed) and two model findings.

**Harness bugs found and fixed:**

1. **Unregistered sessions → HTTP 400.** `build_http_dispatch` used a
   per-scenario `session_id` (`e2e-todo-0`, ...) that the gateway
   rejected with `unknown_session`. Every scenario failed with
   `upstream_error` before reaching the model. Fix: the dispatch now
   registers the session via `session_registry.create` (idempotent)
   before driving it.
2. **No isolation → real-data pollution + stale false-positives.** The
   run used the operator's real `~/.fitt`, so `todo_add` wrote "call the
   doctor" into the *real* todo list (10× — see finding below), and a
   later run's objective check PASSED off that stale item even though
   that run's model never called the tool. Fix: `fitt eval e2e` now runs
   under an isolated scratch `FITT_HOME` (+ redirected `memory` dirs);
   only the DUT endpoint/aliases come from the real config. Objective
   checks are now causally valid (empty stores at start → only this run
   can populate them).
3. **ASK-bucket tools can't execute (no approver).** The reminder
   scenario's `cron_add` is an ASK-bucket tool; with no human to tap
   approval it blocked to timeout and rejected — the scenario could
   never pass. Fix: the eval app wraps `app.state.approval` in
   `_AutoApproveWrapper` (same posture as the cron/profile runners; deny
   list still enforced).
4. **Invalid session ids from underscores.** Scenario names with
   underscores (`news_summary`, `memory_recall`) produced session ids
   that failed the `^[a-z0-9][a-z0-9-]*$` validator, so those sessions
   silently didn't register and every turn 400'd — masked by an
   over-broad `except` in the dispatch's session-ensure. Fix: sanitize
   the id (non-conforming char → hyphen) in the CLI, and narrow the
   dispatch's `except` to only the benign duplicate so a real
   validation error surfaces (Principle 11: fail loud). The
   per-scenario diagnostic block in the written report (loop_status /
   error / tools / reply) is what made this visible.

**Model findings (both DUTs fail to cleanly drive tools in a plain turn):**

- **qwen3:14b (thinking) tool-loops to exhaustion.** On "add a todo" it
  called `todo_add` on *every* iteration and hit `tool_loop_exhausted`
  (10 iters), writing the item 10×. Base prompt was only ~4,095 tokens
  (NOT prompt bloat — cf. the prompt-size entry below); the context grew
  to ~40,950 (the model's 40,960 ceiling) purely from loop accumulation
  of its heavy `reasoning_content` (186 output tokens just to say
  "Hello"). This is a thinking-model loop-control problem, not a
  prompt-size one.
- **hermes3:8b narrates tool calls.** It emitted the call as JSON in
  `content` (`{"name": "todo_add", ...}`) with no real `tool_calls`
  structure on the reminder/news turns — the known narrated-tool-call
  failure (see hallucinations doc, problem A). It *did* emit a real
  `todo_add` on the todo scenario (2 iterations, executed), so it's
  inconsistent rather than incapable.

A clean hermes3:8b run (all fixes in) scored **1/4 objective** and made
the harness's whole point concrete: hermes *hallucinated success* on 3 of
4 — it replied "I have added your reminder … due tomorrow at 9am"
(no `cron_add`), fabricated plausible "Search results for …" (no
`web_search`), and invented a hub redeploy procedure (no
`memory_search`) — while only the **todo** case actually executed a tool
and moved real state. A reply-only judge would have passed all four; the
objective side-effect check caught the three fabrications. This is
exactly the "did it actually get it done, not just look right"
verification the harness exists for.

**Judge validated live (2026-08-07).** The frontier judge runs via
`--judge --judge-command "kiro-cli chat --no-interactive"` — kiro-cli
reads the prompt on stdin, exits 0, and prints the verdict (a leading
`> ` + a credits banner that the first-`{...}`-block parser strips).
Two gotchas worth remembering: (a) a terse "reply with ONLY this JSON"
prompt trips kiro-cli's prompt-injection guard and it refuses — a
properly-framed grading prompt (task + rubric + reply, which
`build_judge_prompt` produces) does not; (b) trust no tools
(`--trust-tools=`) so the judge only reads and answers. On the hermes
run the judge **independently agreed with the objective layer** —
FAIL on the three fabrications, PASS on the real todo (judge 1/4 ==
objective 1/4), which is the cross-check we wanted.

**Cost / next:** the harness now works end to end (objective + judge)
and faithfully reports these — its stated purpose (a dev/debug driver to
*drive* feature work). Neither tunnelled DUT is a clean tool-driver in a
single chat turn today; the harness is the right instrument to measure
that as models/prompts change. Web-search + retrieval were off in this
config, so news/memory scenarios can't pass until those are configured —
expected, not a harness fault.

---

## Prompt-size budget: total tokens vs the model's degradation threshold

**First observed:** granite 2026-05-22; framing written down 2026-07-02
after re-deriving it for the third time.
**Tag:** model-fit / prompt budget / the mental model we keep losing.

The recurring confusion, settled. Small/quantized local models lose
tool-call discipline as **total prompt size** grows — well before the
context-window ceiling. Granite 3.3 8B: clean structured `tool_calls`
at ~141 prompt tokens (no system prompt), narrated JSON at ~5K tokens
(FITT's full prompt), on a **256k** window. So it's a *quality* problem
at ~2% of capacity, not an overflow problem. Position/content matter
secondarily ("lost in the middle"); total size is the dominant driver.

What the prompt is made of, per turn:

- **Fixed overhead** — capability block + skills + identity + lessons,
  injected *every* turn. Does NOT accumulate across turns (each request
  is stateless); it's a flat per-turn tax.
- **History** — accumulates, but is **capped** by `max_history_chars`
  (~6K tokens; oldest turns truncated past it). So it **plateaus** — it
  does not grow forever. Turn 10 and turn 1000 are about the same size.
- **Prefetch** — Phase 9 `[Recalled context]`, opt-in, bounded.

So per-turn total plateaus at ≈ `fixed overhead + history cap`. Two
consequences people (us) get wrong:

1. "System becomes negligible as history grows" is true as a *ratio*
   and irrelevant — the model cares about the *absolute total*, which
   only climbs to the plateau.
2. **Trimming the system prompt buys headroom, not a cure.** If the
   plateau (fixed + history budget) still exceeds the model's
   threshold, you degrade permanently after a few turns regardless. You
   have to keep the *total* under threshold.

KV/prefix cache is a red herring here: it skips *recompute* of a stable
prefix (speed/VRAM), but the tokens still occupy the window and every
generated token still attends over all of them — no help for the
quality degradation.

**The single question:** is FITT's plateau (fixed overhead + injected
history) under the bound model's degradation threshold? Levers to keep
it there:

- Trim fixed overhead (`tools.compact_capability_block`-style block
  trimming) — lowers the floor.
- Lower `max_history_chars` — lowers the ceiling, but crudely (drops
  turns).
- **Retrieval (Phase 9, shipped)** — keep injected history tiny, full
  transcript stays on disk — lowers the ceiling *without losing
  precision*. The good one.
- Compaction (Phase 8, unbuilt) — fit more meaning per token (lossy),
  for when even lean history is too big.

The number that grounds all of it — *where this model degrades* — is
the "context-degradation curve" (a backlogged capability-profile
dimension). Measure it → set the budget deliberately. See the backlog
item "Monitor prompt size against the model's input budget."

**Refinements from the 2026-07-02 thread (would this have helped
granite? mostly no — a useful check on our own solutions):**

- **Which lever works depends on WHERE the bytes are.** History-
  dominated bloat → retrieval (precision) or compaction (lossy).
  Fixed-overhead-dominated (granite: ~5K was capability + skills +
  identity + lessons, ~no history) → trim the blocks
  (`compact_capability_block`-style) or swap the model. So retrieval
  and compaction — the two big memory features — **would not have
  fixed granite.** Earlier framing over-sold retrieval as the general
  answer; it's the answer for *history*, not FITT's fixed per-turn tax.
- **The durable granite fix was model choice**, not a prompt-size
  lever: `choosing-a-model.md` says ≤8B is fine for chat-only aliases
  or small-prompt tool turns, not a tool-heavy default. Prompt-size
  levers extend a marginal model's runway; they don't rescue one
  already at its limit with the irreducible prompt.
- **Monitoring must compare against the measured threshold, not the
  window.** A size-vs-window monitor would have shown granite GREEN
  (5K of a huge window looks fine) — that's the trap. What actually
  catches granite today is the *realistic eval suite* (runs the real
  prompt, observes narration); the missing piece is the per-model
  threshold number for a live guardrail.
- **Coding-agent "%-of-window" compaction triggers assume degradation
  ≈ capacity.** True for frontier models (they hold quality to near
  their window), false for small local ones (granite degrades at ~2-4%
  of window). A 95%-of-window trigger fires *far too late* for FITT's
  model class. **FITT's Phase 8 draft trigger inherits this bug** —
  reshape it to a degradation-based trigger, not a capacity %.
- **Advertised context ≠ effective budget, and it's partly your own
  config.** The "2% of 256k" denominator is inflated: 256k was an
  operator-set `OLLAMA_CONTEXT_LENGTH`, not granite's native. Two
  separate capabilities: *hold/retrieve N tokens* (needle-in-haystack,
  what the spec advertises) vs *tool-call discipline under a dense
  instruction prompt* (collapses far earlier, worse on 8B + Q4).
  Untested hypothesis worth a 10-min check: over-extending `num_ctx`
  beyond native can trigger RoPE position-scaling that degrades quality
  at ALL lengths — so a *smaller* `OLLAMA_CONTEXT_LENGTH` might recover
  granite's short-prompt tool-calling. The config you pick is itself an
  input to where the model degrades.

---

## Embeddings run on CPU-only Ollama; Phase 9 recall validated locally

**First observed:** 2026-07-02 (Phase 9 V1 validation).
**Tag:** Phase 9 / memory-v1 / retrieval quality.

The local Ollama (`localhost:11434`) can't run chat models (CPU-only,
refuses generation) — but it runs `nomic-embed-text` embeddings fine
(dim 768). Embedding is a single forward pass, not autoregressive
decoding, so it's far lighter than chat. **Implication:** FITT's
retrieval subsystem needs no GPU/EC2 — the embedding model lives happily
on the CPU hub.

Validated recall quality (V1/U1.4) end to end with real embeddings:
indexed synthetic multi-week turns, and the semantic query "what did we
decide about the training crashes?" ranked a **3-week-old** CUDA-OOM
turn #1 (score 0.616) over unrelated noise — i.e. a turn well outside
the recency-injection window is recalled by meaning. Keyword search,
cross-session scope, session isolation, and the prefetch block all
confirmed. Phase 9 shipped.

**Operator note (home deployment):** to turn it on, add the embed model
+ an alias in `~/.fitt/config.yaml` and set `memory.embedding_alias`:

```yaml
models:
  - id: local-embed
    backend: ollama
    model: nomic-embed-text
    endpoint: http://<ollama-host>:11434
aliases:
  fitt-embed: local-embed
memory:
  embedding_alias: fitt-embed
```

Then `fitt memory reindex` to backfill, `fitt memory status` to check.
In Docker, the endpoint must be reachable from the gateway container
(MagicDNS name / host IP, not localhost).

---

## Phase 9 substrate: Honcho evaluated (desk research), rejected for v1

**First observed:** 2026-07-02 (Phase 9a spike, short-circuited by
desk research before infra).
**Tag:** Phase 9 / memory-v1 / substrate decision.

The Phase 9 spec made Honcho (plastic-labs) the P0 substrate spike.
Reading the primary docs (Honcho server v3.0.9, self-hosting guide)
answered the fit question without standing it up:

- **Weight.** Self-host = FastAPI API + a background *deriver*
  worker + Postgres/pgvector. Two-to-three services, not one.
- **Cloud LLMs by default.** Deriver/summary/dialectic default to
  Gemini + Anthropic; embeddings to OpenAI. Fully-local (Ollama
  embeddings, llama.cpp) is a non-default, community-patched path —
  friction with Principle 5 (local, no subscription).
- **AGPL-3.0.** Fine for FITT calling it over HTTP (network
  boundary, doesn't infect FITT's code); noted for the record.
- **Value-add is a v1 non-goal.** Honcho's differentiator is the
  reasoning layer (peer representations, conclusions, dialectic
  chat). Phase 9-v1 explicitly scoped that out; v1 wants keyword +
  vector *search* over FITT's own markdown.

**Decision:** build the home-grown `LocalRetrievalProvider` (SQLite
FTS5 + embeddings, brute-force cosine) against the `RetrievalProvider`
ABC. Deployment-neutral, dependency-light, does exactly what v1
needs. **Cost:** none — the ABC was designed for exactly this fork,
so only the wired module changes. **Revisit:** Honcho becomes
interesting again if FITT later wants automatic user-model synthesis
/ conclusions (a deliberate v1 non-goal today).

---

## Phase 12.6 re-baseline: real-registry eval at parity with lookalikes (gemma4)

**First observed:** 2026-07-02 (Phase 12.6d re-baseline over the SSM
tunnel to EC2).
**Tag:** Phase 12.6 / eval-over-real-registry / measurement ladder.

After 12.6a-c switched the default + realistic suites to offer FITT's
*real* registered tool schemas (via `tool_names` + `resolve_case_tools`)
instead of hand-written lookalikes, profiled `fitt-ec2-gemma4`
(gemma4:12b-it-qat, over the tunnel) on the wired path:

- **tool-calling: 100% (30/30)**, p50 3.1s / p95 7.9s.
- **coding: 76% (19/25)** — synthetic suite, unchanged by 12.6.
- **plan-election: 100% (5/5)**, p50 23.4s.

The finding is a *non-finding*, which is the point: the real schemas
performed **at parity** with the lookalikes — no tool proved genuinely
harder once it faced a model in its shipped shape. The design (Decision
6) said "expect the numbers to move"; for gemma4 they didn't, so the
switch is a clean re-baseline with no regression and no tool-ergonomics
surprise to chase.

**Cost:** none — this is the known-good baseline going forward.

**Caveat worth keeping:** the coding suite's per-case failures
(`code_edit_basic` reads before it edits 5/5; `code_shell_basic`
intermittently emits a malformed `"tool_calls:"` tool name) are
**single-turn measurement artifacts**, not capability grades. Reading
before editing is what an agent *loop* wants; the single-turn eval
can't see the second turn. Frontier coding agents moved from prompt
design to loop design, and FITT's coding suite only exists to sanity-
check router-mode bindings whose external agent brings its own loop. So
treat the coding pass-rate as a coarse "does this binding tool-call
under prompt pressure" signal — single-turn tool-election is not loop
capability. Attribution to model-vs-prompt would need per-case ablation,
which doesn't scale and isn't worth it here.

---

## Liveness bullet conflates fresh-shallow reachability with stale-deep probe; nothing auto-refreshes

**First observed:** 2026-07-01 (walking the ping/probe/eval/profile
vocabulary; see project-overview "measurement ladder").
**Tag:** Phase 7.6 / probe clarity / dashboard freshness.

Two findings from reading the reachability + probe wiring:

1. **The colored alias bullet shows the probe (a tool call), not
   reachability.** `_probe_pip(probe.status)` drives it. So "green"
   means "last tool-call probe was ok", not "host is up right now".
   The two answer different questions: reachability is cheap and
   shallow (a granite-shape model is reachable but narrates -> would
   show green wrongly); the probe is deep but expensive.
2. **Nothing refreshes either signal on a schedule.** The probe runs
   at boot (`_run_boot_probe`) and on manual re-probe only - no
   periodic task (cron/pruners/context-populate are the only loops).
   Reachability (`reachability.check_reachable`) is recomputed fresh
   only when `/ready` is hit, and isn't stored on the alias state the
   dashboard reads. So on a long-lived gateway the bullet reflects a
   boot-time probe that can be days old - which is exactly why 12.5b
   added a "stale" flag to the liveness line.

**Cost:** a stale-green bullet can imply an alias is healthy when it
isn't (or vice versa) - low-frequency but misleading during an
incident.

**Fix shape:** decide what the at-a-glance bullet is *for*. If it's a
live "is this up" dot, back it with the cheap signal (reachability)
and add a small periodic ping so it's actually fresh; show the deeper
probe verdict as a separate, timestamped line ("tool-call: ok, 2d
ago"). If it stays probe-backed, surface staleness everywhere (the
alias page does now; the aliases-table bullet still doesn't). The
underlying gap is "no background refresh of either signal" - fix that
before bikeshedding the color. Belongs to the `phase7.6-probe-clarity`
lineage (owns pip semantics + the amber/green/red rules), not the
eval-harness thread this surfaced from.

**Urgency:** low. A note, not a fire.

## Capable model (qwen3:14b) synthesizes from real search content - relay was model weakness, not prompt

**First observed:** 2026-06-26 (synthesis-vs-relay retest; BACKLOG follow-on
to the task-26 verdict).
**Tag:** Phase 12 / synthesis vs relay / model-fit / web_search.

Retested the task-26 relay-vs-synthesize question on a *capable* model with
*real* content. Added a `topic_brief` scenario - a specific query ("recent
discoveries from the James Webb Space Telescope") concrete enough that ddgs
returns content-rich results (per the ddgs finding above), removing the
thin-search confound that muddied the hermes3 runs. fitt-ec2-qwen3
(qwen3:14b), flat, 3 samples.

Result: 2/3 `completed`; the deciding signal is reading the replies -
**both content-bearing samples SYNTHESIZED.** Clean 3-5 bullet summaries in
qwen3's own words with inline source attribution (Carnegie Science /
GLASS-JWST, NASA NIRSpec / comet 3I/ATLAS, ESA Butterfly Nebula,
ScienceDaily early galaxies, ...) - NOT a relay of raw title/URL/snippet
lines. The opposite of hermes3:8b's relay. Sample 2 also *retried* a failed
search (`web_search:error -> web_search:ok`) - agentic recovery hermes3
never showed.

**Verdict: the relay-vs-synthesize failure is MODEL CAPABILITY, not the
prompt.** A capable model synthesizes given real content under the *existing*
capability-block guidance; hermes3:8b relays because it's too weak, not
because the prompt is wrong. This confirms the task-26 caveat and refines
the Phase 12 conclusion: the lever for output quality is a capable executor,
not harness/prompt tuning. (It also retroactively justifies reverting the
"synthesize, don't relay" capability-block change - it wasn't needed; the
prompt was already adequate.)

Caveats / observed oddities:

- **ddgs flakiness recurred**: `web_search` returned `provider_failed` on the
  first call in 2 of 3 samples (rate-limiting). qwen3 recovered by retrying
  (sample 2); sample 1 never got results.
- **Off-topic hallucination on total search failure (sample 1):** with the
  search fully failed, qwen3 acknowledged "the web search failed" but then
  pivoted to an unrelated **cake recipe** instead of staying on JWST or
  honestly stopping. A bizarre n=1 oddity - when it has no data it should say
  so, not generate unrelated content. Worth watching.
- n=3, one topic, one model. A clear signal (2/2 content-bearing samples
  synthesized), not a law.

Tooling: added the reusable `topic_brief` scenario
(`fitt scenario run <alias> --scenario topic_brief`).

---

## ddgs returns homepages for generic news queries: query shaping, not the backend

**First observed:** 2026-06-23 (Phase 12 synthesis experiment).
**Investigated:** 2026-06-26.
**Tag:** web_search / ddgs / query shaping.

The daily_news_summary experiment saw `web_search` return news-site
homepages (Google News / NBC / CBS / NYT) instead of headlines.
Investigated by probing ddgs directly (the search runs on the gateway
host - no model involved), four queries:

1. `text("today top news headlines")` -> HOMEPAGES (Google News, AP,
   WaPo, NBC, CNN) with boilerplate snippets. Reproduces the problem.
   Caveat: a couple snippets *did* carry a real headline (AP/Google's
   "Venezuela earthquakes kill at least 235"), so a model that reads
   snippets could extract *some* news - the weak model relaying the
   raw list is a compounding factor.
2. `news("top news today")` -> ERROR. ddgs's news endpoint routes via
   Yahoo (`news.search.yahoo.com`) and returned a DNS "Query Refused".
   The dedicated news endpoint - the right tool for headlines - is
   broken in the current ddgs.
3. `text("world news", timelimit="d")` -> a single Wikipedia "World
   news" concept page. timelimit doesn't help.
4. `text("Iran nuclear agreement news")` (SPECIFIC) -> relevant,
   content-rich results (Wikipedia 2025-26 Iran-US negotiations, CFR,
   a Jun-2026 video, ICAN). A specific query returns useful results.

**Diagnosis: the homepage problem is QUERY SHAPING, not a backend
limitation.** ddgs `.text()` returns rich, on-topic results for
specific queries and homepages for generic "today's news" ones. The
`.news()` endpoint that would fix the generic-headlines case is broken
(Yahoo DNS refused); `timelimit` is no help.

**Implications:**

- Not a small provider code fix. Levers: (a) get the model to issue a
  *specific* query - hard for the inherently-broad "summarize today's
  news"; or (b) a working news backend (DDG news is broken here; a real
  news API or a different provider would be needed - the provider plugin
  layer makes that a config + one-file add).
- For the synthesis-vs-relay retest (the other open item): use a
  SPECIFIC-topic query so there's real content to synthesize. That
  removes the thin-search confound and makes it a clean test of
  synthesis vs relay, decoupled from this ddgs limitation.

**Tooling:** probed with a throwaway `ddgs_probe.py` against the venv's
ddgs (deleted after).

---

## Synthesis-over-relay capability-block tuning didn't help hermes3:8b (reverted)

**First observed:** 2026-06-23 (test of the task-26 "under-prompted" verdict).
**Tag:** Phase 12 follow-on / capability-prompt tuning / web_search quality.

Task 26 concluded the daily_news_summary failure was "under-prompted"
and pointed at strengthening the synthesis instruction. Tested it.
Note: orchestration is off by default, so the live flat-loop lever is
the capability block (`capabilities.py`, the [Using tools for current
facts] section), NOT the execute-step prompt resolver (which only
fires in planned/orchestrated mode). Strengthened that block: read the
snippets and SYNTHESIZE in your own words, do not paste/number/list
titles/URLs/'Snippet:' lines, and if the results don't contain the
answer say so + suggest a better query.

A/B on fitt-ec2-hermes (hermes3:8b), flat, 3 samples each, same day,
reading the actual replies (the length classifier scores all as
`completed`):

- Baseline (old block): 0/3 synthesized - one send_message-schema
  derail, one verbatim relay, one meta-relay.
- Treatment (new block): 0/3 synthesized, arguably worse - all 3
  abandoned the news task to narrate tool schemas (todowrite /
  project_shell / learn_remove).

**Verdict: the "under-prompted" hypothesis is NOT supported on
hermes3:8b.** A stronger synthesis prompt didn't help and plausibly
hurt - consistent with the documented "longer prompt degrades
hermes3:8b" effect (see the planner tool-blindness entry) and the
broader Phase 12 conclusion that the 8b is the bottleneck, not the
harness. **The capability-block change was reverted.**

Two confounds keep this from being a clean test:

1. **Search quality.** ddgs returned news-site HOMEPAGES (Google
   News / NBC / CBS / NYT) with boilerplate snippets, not actual
   headlines (task 26 days earlier got real headlines like "Trump
   says agreement with Iran is not final"). With no real content
   there is nothing to synthesize, so relay-vs-synthesize can't be
   judged. A prerequisite to fix before retesting.
2. **Model weakness / prompt load.** hermes3:8b's tool-schema-
   narration degeneracy dominates at n=3.

**A clean retest needs both:** a capable executor (qwen3:14b) that can
actually obey "synthesize, or honestly report thin results", AND a
search query/backend that returns real headlines. Until then the
prompt change stays out of the tree.

**Kept:** the harness change that made this readable - a
`--preview-chars` option on `fitt scenario run` (default 200) so the
operator can read full replies, since only the text (not the
length-based pass rate) reveals synthesis vs relay (the task-4
mandate, "read the actual replies").

**Caveat:** n=3, one model, one task, garbage search input,
EC2-over-SSM (flaky). A clear signal, not a law.

**Urgency:** low. Negative result logged so it isn't blindly re-run;
the real follow-on is execute-step prompt tuning measured on a model
that can follow it, with working search.

---

## Open WebUI model picker went empty: PersistentConfig pinned the stale gateway port

**First observed:** 2026-06-23 (operator: "can't select a model in
Open WebUI anymore").
**Tag:** deployment / Open WebUI / PersistentConfig / config-as-code.

The Open WebUI model dropdown went empty. The gateway was healthy
and `GET /v1/models` returned the alias list correctly end to end -
proved by curling it from *inside* the open-webui container
(`docker exec fitt-open-webui curl -s http://gateway:8421/v1/models`
-> full JSON). The OWUI logs told the real story:

    ERROR [open_webui.apps.openai.main] Connection error:
    Cannot connect to host gateway:8080 ... Connect call failed

Open WebUI was dialing **`gateway:8080`** - the gateway's *old*
port - while the gateway now listens on 8421. Root cause:
`OPENAI_API_BASE_URL` is a **PersistentConfig** variable in Open
WebUI. The compose env (`http://gateway:8421/v1`) only SEEDS the
OWUI database on first boot; after that OWUI reads the value from
its own DB and ignores the env. The gateway had moved off port 8080
(it collides with QNAP's QTS admin UI - the exact collision
`.env.example` warns about) to 8421, but OWUI's DB kept
`gateway:8080`. Env said 8421, DB said 8080, DB won.

Red herring along the way: `curl http://nas-qnap:8080/v1/models`
returns a 302 to `https://nas-qnap:443/...` - that's QTS's admin UI
answering on 8080, not the gateway, and unrelated. The gateway is on
8421 and OWUI reaches it over the compose network, never the host
port.

**Cost:** a long debug. Every symptom pointed at the gateway
(healthy, yet an "empty" external curl) when the gateway was fine -
the failure was hidden state in OWUI's DB that no config file
revealed.

**Immediate fix:** edit the connection URL in OWUI Admin -> Settings
-> Connections to `http://gateway:8421/v1`. Works, but it's
click-ops living in the DB - it silently breaks again on the next
port change, volume reset, or fresh hub.

**Durable fix (shipped):** set `ENABLE_PERSISTENT_CONFIG=false` on
the open-webui service so OWUI re-reads its env on every boot and
the compose file is authoritative again. The compose env already
points at `gateway:${FITT_PORT}/v1`, so the connection self-corrects
and can't drift. Ripple: `ENABLE_SIGNUP` is also PersistentConfig,
so its UI toggle stops persisting - moved it to a declarative
`WEBUI_ENABLE_SIGNUP` .env knob (default false, fail-secure) with a
two-phase bootstrap, and rewrote quickstart step 16 to match. Trade:
Admin-UI config changes no longer survive a restart (intended for a
config-as-code hub; accounts and chats are unaffected - they are not
PersistentConfig).

**Lesson:** any Open WebUI setting exposed as an env var is
PersistentConfig by default - the env is decorative after first boot
unless `ENABLE_PERSISTENT_CONFIG=false`. Treat OWUI as config-as-code
from the start, or moving any of those values later silently no-ops.

**Urgency:** resolved (durable fix shipped). Confirm the flag is
honored on the pinned image after the next
`docker compose up -d open-webui` (v0.3.35; PersistentConfig shipped
in 0.3.0, so it is).

---

## Phase 12 verdict on daily_news_summary: the failure is the prompt, not the harness or the model

**First observed:** 2026-06-16 (Phase 12 task 26, live-validation close-out).
**Tag:** Phase 12 hypothesis verdict / planning value / prompt design.

Task 26 closes the Phase 12 measurement sweep with a verdict on the
guiding hypothesis — *"a weak model is under-harnessed, not incapable;
elected planning makes it competent on a multi-step turn."* Five live
experiments on `daily_news_summary` (fetch today's news, then summarize)
across hermes3:8b and qwen3:14b on EC2 (mirroring the hub's lineup):

| Experiment | Config | Result |
|-----------|--------|--------|
| Task 4 | hermes3 flat | 5/5 fetched, 0/5 synthesized (relay) |
| Task 22 | hermes3 planned | no delta vs flat |
| Task 23 | hermes3 as planner | plan election 0% |
| Task 25 | qwen3 plans, hermes3 executes | election 0->100%, execution still relay |
| Task 26 | qwen3 flat | marginally better, still mostly relay |

**The hypothesis did NOT hold cleanly for this case.** Walking the
levers:

- **Better harness (planning) didn't help** (22). hermes3 doesn't even
  elect to plan (23); when a capable planner forces a correct,
  re-injected plan (25), hermes3 still relays — so the bottleneck
  isn't sequencing/election, which is what planning's leverage is.
- **A more capable executor helped only at the margin** (26). qwen3:14b
  pulled some real headlines ("Trump says agreement with Iran is not
  final") and recovered from a failed search, where hermes3 never did
  — but 2/3 of its runs still relayed source listings/snippets.

The one factor invariant across every model and every harness is the
**relay-vs-synthesize tendency**: `web_search` returns titles + URLs +
snippets, and the models default to reformatting that structure rather
than reading it and writing original prose. That points at the
**execute-step prompt** (and the tool-result shape), not a capability
cliff or a harness gap.

**Verdict: for this case the framing should be "under-PROMPTED", not
"under-harnessed".** The lever is the execute-step prompt (Story 2.4
per-step tuning) — explicitly demand "read these results and write a
summary in your own words; do not list sources" — not planning and not
a model swap. Capability does matter at the margin (qwen3 > hermes3),
so the task-24 capability profile is still worth building; but it is
not the lever for *this* failure.

**Scope / honesty caveat (the n=1 discipline, applied):** this is ONE
multi-step task, n=3-5 per config, EC2-over-SSM (which dropped tunnels
mid-sweep — the runner now records those as transient and excludes
them). It is a clear, consistent signal, NOT a phase-wide law. A
sequencing-heavy task (where the failure IS step ordering, not prose
quality) would likely show planning earning its keep — that direction
is untested here and is exactly what a broader task-24 case set should
cover. The classifier limitation compounds the caveat: `completed` is
length-based and scores relay as pass, so this verdict rests on
reading the replies, not the pass rate.

**Actionable follow-ons (not this phase):**
1. Tune the execute-step prompt to demand synthesis over relay; re-run
   the sweep — the cheapest test of the "under-prompted" verdict.
2. task 24: per-dimension capability profile, including a sequencing-
   heavy case so planning's value is measured where it should appear.

---

## Planner_alias split (qwen3 plans, hermes3 executes): fixes election, not execution

**First observed:** 2026-06-16 (Phase 12 task 25, "concentrate intelligence in planning").
**Tag:** Phase 12 planner_alias / planning value / model-fit.

Task 25: the "concentrate intelligence in planning" experiment —
planner on qwen3:14b (capable, thinking), executor on hermes3:8b —
on `daily_news_summary`, 3 samples, via `fitt scenario run
fitt-ec2-hermes --mode planned --planner-alias fitt-ec2-qwen3`.

| Metric | hermes plans (task 23) | qwen3 plans (task 25) |
|--------|------------------------|-----------------------|
| plan election | 0% | **100%** |
| actually synthesized | 0/5 | 0/3 |
| pass rate | n/a (no plan) | 2/3 |

**The capable planner fixes the planning step, not execution.**
qwen3 produced a plan every time (election 0% -> 100%), and the
orchestrator re-injected it into hermes3's executor context. But
hermes3 still relayed raw search results instead of synthesizing
(2/3), and one run degenerated entirely (`no_search`): hermes3
emitted no tool calls and dumped pydantic JSON schemas of tools it
"will make" into the user-facing reply — 7 iterations, 21K tokens,
no actual search.

So "concentrate intelligence in planning" doesn't rescue a weak
executor on this task. The plan was present and correct; hermes3
just isn't capable enough to follow it through to a synthesized
answer. **The lever for this failure (execution output quality) is
a more capable executor, not a better plan** — planning's leverage
is sequencing/election, which wasn't the bottleneck here.

This refines the Phase 12 hypothesis ("elected planning makes a weak
model competent on multi-step turns"): planning helps when the
failure is *sequencing* (does it plan, does it call tools in order);
it does not help when the failure is *execution output quality*
(does the model write a good answer from tool results). The
daily_news_summary failure is the latter.

Caveat (n=1 discipline): 3 samples, one task, one model pair. A
clear, consistent signal (100% election, 0% synthesis), not a law.
The systematic per-dimension read is task 24.

---

## Flat-vs-planned comparison on daily_news_summary: no delta on hermes3:8b

**First observed:** 2026-06-16 (Phase 12 task 22, flat-vs-planned comparison).
**Tag:** Phase 12 hypothesis test / planning value / model-fit.

Task 22: same `daily_news_summary` scenario, same alias (hermes3:8b on
EC2), 5 samples flat vs 5 samples planned, via `fitt scenario run
--mode flat` / `--mode planned`.

Both modes score 5/5 `completed` (searched + substantial reply). But
reading the replies: **both relay raw search results instead of
synthesizing.** Planning did not fix the output quality gap.

| Metric | Flat | Planned |
|--------|------|---------|
| web_search called | 5/5 | 5/5 |
| send_message called | 1/5 | 1/5 |
| Actually summarized | 0/5 | 0/5 |
| Avg iterations | 2 | ~4.6 |
| Avg in_tokens | ~8190 | ~9428 |

Planning added overhead (~2.6 extra iterations, ~1.2K more input
tokens per sample) but the failure mode — "relay links/snippets
verbatim instead of writing a bullet summary" — persists unchanged.
One planned sample [5] went off the rails and dumped the `todowrite`
JSON schema into the user-facing reply.

**Why planning didn't help:**

1. The flat-loop failure here is NOT structural (missed tool
   calls / wrong sequencing). hermes3 already fetches reliably
   without a plan. Planning's leverage is multi-step sequencing;
   when the model sequences fine but writes badly, planning adds
   cost without moving quality.
2. hermes3:8b may be too weak to synthesize regardless of
   harness — the "actually-incapable" possibility task 26 tests.
3. The untested lever: a `planner_alias` split (qwen3 plans
   explicitly "summarize results into bullets", hermes3 follows
   the explicit instruction). That's the task-25 experiment.

**Classifier limitation confirmed:** both modes score identically
on the structural pass-rate — the task-4-noted limitation
(length can't tell a summary from a relay) manifested exactly as
predicted. The comparison requires reading the actual replies.

---

## Flat-loop baseline on daily_news_summary: fetches but relays raw results instead of summarizing

**First observed:** 2026-06-16 (Phase 12 task 4, the flat-loop baseline read).
**Tag:** Phase 12 flat-loop baseline / model-fit / eval classification.

The task-4 read: ran the *current flat loop* (no planning) on the
`daily_news_summary` scenario — "search the web for today's headlines,
then give me a 3-4 bullet summary; push it if you can" — against
hermes3:8b on EC2, 5 samples, via the new `fitt scenario run
fitt-ec2-hermes --mode flat`.

What we saw:

- **5/5 called `web_search` successfully.** No "I can't access
  real-time data" refusal, no answering from stale training data. The
  *fetch* step is solid on hermes3 — the failure is not where the
  spec's running example assumed ("doesn't fetch").
- **0/5 actually summarized.** Every reply relays the raw search
  output — "here are the search results: 1. Fox News — URL: ... Snippet:
  ..." — instead of the requested bullet summary. The capability
  prompt explicitly says "don't just relay a list of links"; hermes3
  ignores it. **This is the real flat-loop failure on this case:
  fetch-then-relay, not fetch-then-synthesize.**
- **`send_message` is unreliable: 1/5 called it** (the prompt said "if
  you can"), and the dev box has no push channel
  (`send_message.no_push_channel`), so even that delivered to a no-op.
- **1/5 went off the rails:** `web_search:ok -> send_message:ok ->
  read_file:error` — after delivering, it made a spurious `read_file`
  call with an unknown project, errored, and narrated the tool error
  back to the user as if answering them.

**Classifier limitation (a finding in itself):**
`scenarios.classify_news_outcome` scored all 5 as `completed` because
the replies clear the 200-char "substantive reply" bar — but they're
link dumps, not summaries. **Reply length cannot distinguish a grounded
summary from a raw-results relay.** Every cheap structural fix (counting
`URL:` / `Snippet:` tokens) is exactly the fragile string-matching the
task-2 conventions say to avoid, so we deliberately did NOT add one.
`completed` therefore means "searched + produced a substantial reply",
NOT "produced a good summary". **Task 22 must read the actual replies,
not just trust the pass rate** — if flat and planned both score
`completed` by length, only reading the text shows whether planning made
it synthesize.

Implication for the planner prompt (Stories 7.1/7.2/7.4): the thing
planning has to fix is the missing *synthesis* step, not the fetch. The
task-22 test is whether an elected plan with an explicit "summarize the
results into bullets" step makes hermes3 actually synthesize rather than
relay.

Tooling: produced by `gateway/scenario_eval.py` + `fitt scenario run`
(the headless multi-sample scenario runner), reusable as-is for the
task-22 flat-vs-planned comparison.

---

## Thinking-model planner stalls: reasoning_content + no tool call reads as "done"

**First observed:** 2026-06-14 (first live orchestrated turn on EC2).
**Tag:** Phase 12 planner pass / agent-loop termination / thinking models.

The first end-to-end orchestrated turn on real models
(`fitt-ec2-hermes`: plan on qwen3:14b via `planner_alias`, execute on
hermes3:8b) ran clean *mechanically* — routing, planner_alias,
executor, web_search, capture all worked — but **no plan was ever
produced**, and the executor ran plan-less (shallow result relay on one
run, a narrated `web_search` JSON-as-text on the next).

Root cause (from the captured cassette, not a guess): qwen3:14b is a
**thinking model**. On the planner pass it emitted **empty `content`,
~1.6k chars of `reasoning_content` (it reasons out the whole plan
in prose), and NO `todowrite` tool call**. `run_agent_loop` terminates
on "no `tool_calls` -> natural stop", so a turn that's empty-content +
reasoning-only + no-tool is indistinguishable from "done": the loop
breaks after iteration 1. The plan never lands in PlanStore.

**`planner_iterations: 2` does NOT fix it** (tested live, hypothesis
disproved): the second iteration never runs, because nothing continues
past a no-tool-call turn. The budget knob only helps a model that
*does* call a tool and needs more round-trips.

So the gap is harness-level, not config:

1. **Planner-level continue-nudge.** When the planner turn returns no
   tool call but has nonzero completion tokens / non-empty
   `reasoning_content` (observable facts, C4-safe), re-prompt once:
   "you reasoned about a plan — now emit it via `todowrite`." This is
   the planner-side analogue of the executor's empty-after-tools nudge.
2. **Possibly carry `reasoning_content` forward** so the model
   continues from its own thinking instead of starting cold on the
   nudge.

Note this is distinct from the 2026-06-11 "tool-blindness" entry below:
that was the planner *refusing on feasibility* (fixed by the
executor-tool hint); this is the planner *thinking but never acting*
under the loop's no-tool-call termination. The tool hint is present
here (the reasoning shows qwen3 correctly planning to use web_search) —
it just never emits the tool call.

Captured fixtures: `~/.fitt/cassettes/ec2-orchestrated-smoke.json`
(budget 1) and `ec2-orch-budget2.json` (budget 2) — both show the
empty-content + reasoning + no-todowrite planner turn. Measured on the
EC2-over-SSM path; a warm qwen3 emitted a single tool call fine on the
boot probe, so this is the planner-prompt/loop interaction, not raw
inability to tool-call.

**Update 2026-06-15 (gemma4:12b-it-qat — the framing was too broad).**
Testing a second thinking model walked this back. gemma4 *mostly
plans fine* (~6-8/10 sampled). Its planner failures are
non-deterministic and not a single "stall":
- **Calls an executor tool from the planner pass.** Caught live:
  gemma4 emitted a `web_search` tool_call (a tool listed only in the
  executor-tools *hint*, not offered in the planner pass) instead of a
  `todowrite`. With budget 1 the loop exhausts with no plan. The
  continue-nudge correctly does NOT fire here (there *was* a tool
  call), so `_is_thinking_stall` returning False is right — but the
  outcome is still no-plan. This is a **side effect of the
  executor-tools hint** (added to stop capable planners refusing): a
  different model reads "here are the execution tools" as "I may call
  them now."
- The qwen3-style empty-content + reasoning + no-tool case also occurs
  occasionally.

Net: the nudge (task 14b) is a **narrow mitigation for one failure
mode on one model (qwen3, n=1)**, not a general fix — "validated live"
was overstated. Characterising planner failure modes per model
belongs in the task-24 capability audit, not ad-hoc onboarding. The
hint's call-the-tool side effect is its own follow-on (the planner
pass arguably shouldn't execute tools it didn't offer).

---

## Planner tool-blindness: capability hint lifts plan-election ~40% -> ~100% (on a capable planner)

**First observed:** 2026-06-11. **Addressed:** 2026-06-12.
**Tag:** Phase 12 task 2.4 / planner pass / eval methodology.

The Phase 12 planner pass offered the model only the `todowrite`
tool. With real models this produced unreliable plan-election:
hermes3:8b and qwen3:14b each emitted a plan only ~2/5 of the time
on a multi-step task ("summarise today's news and send it to me").
qwen3's misses were the tell — it *refused* on feasibility ("I don't
have access to real-time news data or the internet"). The planner
couldn't see that the *execution* step has `web_search`,
`send_message`, etc., so a capable model judged the task impossible
and declined to plan. Prompt micro-tuning didn't help (and a tweak
regressed it); swapping to the bigger model didn't help either — so
it was neither a wording nor a raw-capability problem.

**Fix:** inject the executor's toolset into the planner's system
prompt, framed as "the execution step that carries out your plan has
these tools" (so the model plans steps that *use* them rather than
trying to call them itself). `run_planner_pass` now builds this from
the registry (excluding `todowrite`). qwen3:14b went from 2/5 to
**10/10** plan-election (one run 7/10 with 3 transient empties),
producing clean, tool-grounded plans (web_search -> compile ->
send_message) with clean stops. Validated through the shipped path,
not a prompt hack.

**But it needs a capable planner.** hermes3:8b did NOT benefit
(0/5, n=5): the longer prompt degraded the 8b into emitting the plan
as *JSON text* in the reply or hallucinating a news summary. So the
hint helps a capable planner and hurts the small one. Conclusion:
this is the `planner_alias` lever (design Story 2.2) and the
orchestration-readiness eval dimension (task 24) — **plan with a
capable model (qwen3:14b), execute with the fast one (hermes3:8b)**
(task 25). hermes "feeling better" in daily use is consistent: it's
the strong *executor* (6/6 at direct tool-calling), just not the
planner.

**Eval-methodology note (operator's point).** Inference itself is
flaky — slow models on the EC2/SSM tunnel eat transient timeouts and
empty completions. The eval must categorize each attempt
(PLANNED / NO_PLAN / EMPTY / ERROR-infra) and compute the capability
rate over *valid* attempts, excluding infra/transient failures, and
multi-sample to average noise. The empties here were
non-reproducible (10/10 the very next run) -> transient, not a
capability miss. Caveat: all rates measured on the EC2-over-SSM path
(flaky + slow for qwen3); a stable/home setup will differ, and call
latency correlates with transient-failure exposure (hermes's ~6s
calls dodge what qwen3's ~60s calls catch). The categorization is now
in the multisample harness (`.scratch/run_planner_multisample.py`)
and feeds the task-24 capability profile.

---

## Dev loop was blind to real models; now wired to local + EC2 Ollama

**First observed:** 2026-06-09. **Addressed:** 2026-06-09.
**Tag:** dev-workflow / Phase 12 task 1 (resolved).

Starting Phase 12 (planning/orchestration) surfaced that the
dev/eval loop couldn't exercise a real model: unit tests use
fakes, and the bound models run at home / on EC2, not from the
dev box. For a phase whose correctness *is* real weak-model
behavior (does it plan, does it tool-call under prompt load),
that meant building the prompt-sensitive parts blind — and my
"eval first" instinct was really "I can't see the model and want
to." Diagnosed mid-session (operator caught the framing).

**Addressed** by wiring the existing eval harness
(`fitt eval alias`) to real Ollama backends — no new harness, just
config + reachability:

- A local dev config (`~/.fitt/config.yaml`) pointing at this
  box's Ollama (`qwen3:8b`, `qwen2.5-coder:14b`).
- The EC2 A10G reached over an **SSH local-port-forward**
  (`-L 11435:localhost:11434 ec2-instance-1`) — Ollama stays bound
  to localhost on EC2, no public exposure, no security-group
  change. Pulled `hermes3:8b` + `qwen3:14b` (the home pair) +
  `qwen3:8b`.

First real signal (the payoff):

- `hermes3:8b` (EC2 A10G): **6/6 bare, 6/6 realistic (~970-token
  capability block)**, sub-second/case.
- `qwen2.5-coder:14b` (local): **1/5 bare**, narrated 4/5,
  ~1 min/case — the documented narration failure mode, reproduced
  live with a real model.

**Caveats / open:** the realistic run only reached ~970 tokens
(memory/skills off in the dev config); the documented degradation
was ~5K. A true degradation read needs a full production-size
prompt. Record/replay for deterministic CI (phase12 task 3) is
still to come. Context-tolerance method (declared window as free
bound + measured operating-point + cheap binary-searched probe) is
captured in phase12 task 24.

**Lesson:** get a real model in the loop *before* building
model-sensitive code — the "enabling step, not a baseline ritual"
framing in phase12 task 1. Also: qwen3 is a reasoning model whose
long thinking phase makes `stream:false` calls block a long time
(disable with `think:false` or budget it) — `hermes3:8b` doesn't
think, hence the latency gap.

---

## cron_add couldn't be driven by a small model — and no test could have caught it

**First observed:** 2026-06-08. **Fixed:** 2026-06-08.
**Tag:** tool-schema ergonomics + eval-coverage gap (the
schema half closed; the harness half open).

Asked FITT (Telegram, `fitt-hermes` → `hermes3:8b`) to set a
plain reminder: "remind me to take out the trash at 8pm
tonight." Three `cron_add` calls, all errored, turn gave up.
The turn-detail page told the whole story: call 1 supplied
`message` + `schedule_spec` → `'name' is required`; call 2
supplied `name` + `schedule_spec` → `'message' is required`;
call 3 supplied `message` + `schedule_spec` again → `'name' is
required`. The model oscillated between two of three required
fields and never converged. (Two secondary `hermes3:8b`
weaknesses rode along: it generated past dates — 2022/2023 —
and on the final iteration narrated the tool call as text
instead of emitting a real `tool_call`.)

**Root cause:** `_SCHEMA_CRON_ADD` required three fields —
`name`, `message`, `schedule_spec` — and one of them was
literally named `name`, colliding with the function's own
name. That's a fumble magnet for a small model: three slots to
fill correctly in one shot, with a confusing label on one of
them. `name` was never load-bearing — the cron `id` is the
key; the label is cosmetic and trivially derivable from the
message.

**Fix (schema half, commit cead402):** required reduced to
`[message, schedule_spec]`; `name` made optional and derived
from the message (`_derive_cron_name`) when absent; properties
reordered so the required pair leads; tool description rewritten
to state REQUIRED args explicitly. Regression tests swapped
`test_cron_add_requires_name` for
`test_cron_add_name_optional_derived_from_message` +
`test_cron_add_still_requires_message`.

**Why no test caught it — the real lesson.** There *are*
`cron_add` unit tests, and they passed the whole time. But
every one of them hand-writes a *correct* args dict — they
prove the handler works when given good arguments, which can
never surface a schema that a *model* can't fill. The thing
that should catch this is the eval suite ("can this model emit
the right tool call?"). But `alias_eval.py` /
`alias_eval_coding.py` test **synthetic** tool schemas declared
inline (`read_file`, `grep_repo`, `list_capabilities`, and in
the coding suite `edit_file`/`glob_search`/`shell`) — they
never load the real registry from `build_cron_tools()` /
`build_fileops_tools()`. So the actual `cron_add` schema, with
its fumble-inducing shape, was never put in front of a model by
any test. It only met one in live use.

**The coverage gap, stated plainly:** our eval harness tests
tools we wrote *for the eval*, not the tools we *ship*. Schema-
ergonomics bugs in the real registry are invisible to it by
construction.

**Audit of the rest of the registry (the "other tools?"
question):**

- `edit_file` — **4 required** (`project`, `path`, `old_str`,
  `new_str`), plus `old_str` must match exactly once. Highest
  remaining fumble surface; the next one I'd expect to thrash.
- `write_file` — 3 required (`project`, `path`, `content`).
- **Naming inconsistency across tools:** `cron_add` calls the
  text-to-say `message`; `send_message` and `learn_add` call it
  `text`. The model hit this live (put `message` where `text`
  was expected → `'text' is required`). Three tools, three
  names for "the words" — a fumble cause in its own right.
- Clean (single obvious required arg): `web_search` (`query`),
  `http_get` (`url`), the cron id-only tools, gitops
  (`project`).

**Fix plan (harness half, open):** extend the eval harness to
run the **real registered tools** (not re-declared synthetic
copies) so schema-ergonomics regressions surface in the
dashboard verdict instead of in a live reminder. When that
lands, normalising the `message`/`text` naming and flattening
`edit_file`'s required set become eval-measurable rather than
guesses. Not started — needs an explicit go and probably its
own small spec.

**Urgency:** the schema fix was high (a personal assistant that
can't set a reminder is failing its core promise) and is done.
The harness extension is medium — it's the systemic fix that
keeps this class of bug from recurring on the next tool.

---

## Tool-schema fumble surface across the registry (edit_file, message/text naming)

**First observed:** 2026-06-08 (audit prompted by the
`cron_add` failure above). **Tag:** tool-schema ergonomics,
open. Standalone entry so these don't stay buried in the
cron writeup.

The `cron_add` fix closed one fumble magnet; the registry
audit turned up two more that no model has thrashed on *yet*
but that have the same shape. Pinning them here so they're
findable on a "what's next" scan instead of living inside
another issue's audit section.

1. **`edit_file` has 4 required fields** — `project`, `path`,
   `old_str`, `new_str` — and `old_str` additionally has to
   match the file exactly once. That's the largest required
   set of any inline tool plus a correctness constraint on one
   field. A small model has to land all four in one shot. This
   is the next tool I'd expect to fail the way `cron_add` did.
   Possible eases: derive nothing here (all four are
   load-bearing), but the exactly-once constraint could give a
   more actionable error that quotes the near-miss, and the
   description could lead with the required set the way
   `cron_add`'s now does. `write_file` is the milder sibling
   (3 required: `project`, `path`, `content`).

2. **"The words" has three different names across tools** —
   `cron_add` uses `message`, `send_message` uses `text`,
   `learn_add` uses `text`. The model already tripped on this
   live (supplied `message` to a `text` slot →
   `'text' is required`). Inconsistency taxes every session
   that touches more than one of these tools. Fix is a rename
   to one canonical name, but it's a breaking change to the
   tool contract, so it wants the eval harness covering the
   real registry first (see the cron entry's open follow-up)
   so the rename is regression-checked rather than hand-waved.

**Cost:** latent. No live failure attributed to these two yet
(beyond the one `message`/`text` slip), but they're the same
class of bug as the cron one, which *did* cost a full failed
turn. The point of recording them now is to fix them before
they're the next live incident.

**Dependency:** both are best done *after* the eval harness
exercises the real registered tools, so the changes are
measured against an actual model rather than asserted safe.
Until then this is a watch-list, not a work item.

---

## Probe flattened "slow / cold-loading" into "transport_error" on a shared-GPU laptop

**First observed:** 2026-05-28. **Fixed:** 2026-06-02
(Phase 7.6). **Tag:** observability / correctness (closed).

Re-probing three aliases (qwen3:14b, hermes3:8b, granite3.3:8b)
that all point at one laptop's Ollama on a 12GB GPU returned
"1 of 3 ok, 2 transport_error". The two failures weren't broken
models — the three probes fired **concurrently** (the old
`probe_all_aliases` used a flat `asyncio.gather` with a "no
contention across aliases" docstring that was false for the
dominant FITT shape), fought over VRAM, and two blew past the
10s timeout while cold-loading. Worse, the failure label was
`transport_error`, which reads like "can't reach the host" —
the exact opposite of the truth (the host was fine, the model
was loading).

**Cost:** Misleading. The operator can't tell "my laptop is
asleep" from "the model is slow" from "the binding narrates
instead of tool-calling" when everything collapses to one word.
Drove a debugging session chasing a network problem that didn't
exist.

**Root cause:** two compounding issues. (1) Vocabulary
fragmentation — the chat path had a mature failure taxonomy
(`upstream_silent` / `upstream_rate_limited` / ...) while the
probe and eval flattened everything to `transport_error`. (2)
Self-inflicted contention — concurrent probes on one GPU
serialise model loads, so probes behind the first time out.

**Fix (Phase 7.6, spec `phase7.6-probe-clarity`):**

- Shared dispatch-outcome taxonomy (`gateway/dispatch_outcome.py`)
  — one vocabulary across chat / probe / eval. `transport_error`
  is gone.
- Reachability-on-timeout: a timed-out canary runs the same
  cheap ping `/ready` uses (`gateway/reachability.py`) and
  reports `upstream_silent` (reachable — slow / cold-loading)
  vs `unreachable` (host down).
- Sequential same-endpoint probing: aliases sharing an endpoint
  probe one at a time; distinct endpoints still overlap.
- Per-probe latency, an amber/red pip split (environmental vs
  broken binding), an endpoint column, and a unified per-alias
  dashboard page (`/dashboard/alias/<id>`) that puts config,
  the shared-GPU "shares with" line, probe detail, and the
  eval suites in one place.

**Urgency at the time:** medium — not a functional outage, but
it actively misled debugging. Closed by living with Phase 7 for
a day (Principle 9) and shipping the follow-up.

---

## Phase 7 live-validation pass — markdown rendering on command outputs

**First observed:** 2026-05-28. **Fixed:** 2026-05-28.
**Tag:** UX (closed), Slice 7.4 follow-up. Caught during the
phase-closeout live-validation pass before flipping the DONE
flag.

The `/model` Telegram command rendered `*Aliases:*` as
literal asterisks on the phone, and the growing turn bubble
showed `Ran \`web_search\`` with literal backticks. Slice
7.4 (Phase 7 markdown renderer) had landed for the streaming
path and turn-bubble flushes (tasks 19a/b), but the deferred
19c (approval prompts) and 19d (command response
constructors) hadn't been touched — and the bubble's
task-line assembly was calling `html.escape` instead of
`markdown_to_telegram_html`, preserving backticks instead of
converting them to `<code>` tags.

**Cost:** Cosmetic, not functional. Replies still made
sense; they just looked unfinished. Caught during the
2026-05-28 validation pass that was supposed to confirm
Slice 7.4's "phone renders correctly" property.

**Fix landed in commit 460f1d4.** Three changes: (1)
`turn_renderer._render_stream_bubble` routes task lines
through `markdown_to_telegram_html`; (2) `handle_session_command`
and `handle_model_command` route their composed markdown
through the renderer with `parse_mode="HTML"`; (3)
`_KNOWN_TOOL_VERBS` registers `web_search`,
`list_capabilities`, `grep_repo`, `glob_search`,
`list_directory`, `http_get` — clean verb pairs instead of
the generic fallback. Five regression tests in the telegram-
bot suite pin each fix.

19c (approval prompts) stays deferred until LLM content
surfaces in approval bubbles. The pre-existing `_format_eval_summary`
/ `_format_status` / `_format_lastturn` already compose HTML
directly, so they were unaffected by the gap.

**Lesson:** "Live-validation pass" caught a real issue the
in-process unit tests couldn't have. The shape of the
issue was a Slice 7.4 deferral that became wrong once
Slice 7.3's commands shipped — exactly the boundary the
deferral was waiting on. Worth respecting "deferred until
X" lists during phase-closeout passes; the deferral
condition often resolved during the phase itself.

---

## Skills property test flaky on hypothesis "no" alphabet

**First observed:** 2026-05-22 during Phase 7/7.2 development.
**Tag:** flaky test, low pain.

`tests/test_skills_properties.py::test_property_scan_failure_isolation`
fails reproducibly when hypothesis generates a skill named
`"no"`. The skills loader rejects the skill (logged
`skills.skipped`) even though the SKILL.md is well-formed. The
test asserts every valid name lands; the rejection breaks the
assertion.

Reproduces on clean `main` without any of Slice 7.2's changes,
so this is a pre-existing skills-loader bug surfaced by
hypothesis's seeded shrinking, not a Slice 7.2 regression.

**Cost:** Low. The full test suite passes ~99% of the time;
only when hypothesis happens to seed the `"no"` example does
it fail. CI will fail intermittently; local devs may not see
it for weeks.

**Fix plan:** investigate why the skills loader rejects a
skill named `"no"`. Likely a `bool(name)` vs
`name == "no"` mistake in the skill validator. Half hour to
locate; another half hour to fix and add a regression test.
Not blocking Phase 7.

---

## Granite 3.3 narrates tool calls under FITT's full system prompt

**First observed:** 2026-05-22. **Tag:** model-fit, medium pain.
Cross-references `docs/choosing-a-model.md` (system-prompt-size
as a model-fit axis) and motivates the Phase 7 visibility work.

`granite3.3:8b` is bound to `fitt-default` on Ollama. Direct
hit against `localhost:11434/api/chat` with a single tool +
no system prompt: clean structured `tool_calls` response,
141-token prompt, 15-token completion, ~2s. Hit through FITT's
gateway with FITT's full system prompt (capability block,
identity, lessons, skills, no history yet — fresh session):
narration in YAML/JSON shape inside `message.content`, no
`tool_calls` field, 5405-token prompt, 103-token completion.
Same model, same Ollama backend, same wire format. The only
load-bearing variable: prompt size, ~38× larger.

**The router-mode (`X-FITT-Client: coding-agent`) test pinned
it.** Through the gateway with FITT's prompt-injection bypassed
and a single user-supplied tool: clean `tool_calls`, 159-token
prompt. So the model is fine; FITT's system prompt is what
flips it.

**Cost:** every Telegram tool-use turn against this binding
narrates instead of dispatching. The agent loop sees no real
`tool_calls` and treats the narrated text as the assistant's
final reply. The user gets a model claiming it ran a tool with
no actual execution — exactly the Problem C (self-deception)
shape the hallucinations doc warned about, surfaced via a
different failure path (model-fit, not regex-narration).

The boot probe (`alias_probe`) didn't catch this because it
fires a 159-token canary; the model passes there. Same data
shape Phase 7's realistic-prompt eval flag is meant to surface.
Same data shape no eval today reports.

**Root cause framing.** Models advertised as "supports tool
calling" pass abstract benchmarks at minimal prompt size.
Discipline degrades with scale. Smaller models (≤12B) lose
structured-output adherence faster than larger ones — the
post-training that teaches "emit `tool_calls`" is fighting the
post-training that teaches "follow long system prompts." The
literature (see `docs/hallucinations-and-poisoning.md` on the
"lost in the middle" effect) frames this as context-window
degradation; for FITT's purposes it shows up well before the
window's ceiling, around 4-6K tokens of system prompt for
8B-class models. The choosing-a-model doc treats this as the
operator-controllable knob; this entry is the concrete
incident.

**Mitigations, ordered.**

1. **Route `fitt-default` to a model that handles long prompts
   with tools.** Per the choosing-a-model doc, `qwen3:14b` /
   `llama3.1:8b-instruct` / `mistral-nemo:12b` are documented
   to handle multi-thousand-token system prompts cleanly; cloud
   models (Claude Haiku, GPT-4o-mini) handle them at any size.
   This is the right answer when reliable tool calling matters
   and is what a future Phase-7-informed binding decision should
   default to.
2. **Phase 7 surfaces this failure mode by default.** The
   per-turn traceability capture (Slice 7.2) logs the
   `prompt_tokens`, `context_window`, and `prompt_pct_of_window`
   for every turn. The Telegram `/model` command surfaces the
   same. An operator hits this case again, sees "5405 tokens on
   a 32k-window model, narrated, finish_reason=stop, no
   tool_calls," and the diagnosis is a glance instead of an
   evening.
3. **Realistic-prompt eval** (deferred to Phase 7+ opportunistic).
   `fitt eval alias <name> --realistic` runs the eval suite with
   FITT's actual injected prompt rather than the bare canary.
   The diff between bare and realistic runs is the diagnostic.
4. **Compact-prompt mode for small models** (Phase 7+
   opportunistic). `tools.compact_capability_block: true` skips
   the prose trailer in the capability block and renders only
   the tool list. Bandage for binding to small models without
   a swap.

**Note on Ollama `num_ctx`.** Operator had set
`OLLAMA_CONTEXT_LENGTH=256k` (the maximum granite supports), so
the prompt reached the model intact rather than being silently
truncated at the default 2048. This *isn't* the bug — but it's
the discoverability gap Phase 7's context-awareness slice
(7.1) addresses: FITT today has no awareness of whether `num_ctx`
is at default, at the operator's override, or at the
architecture ceiling. Without that, compaction (Phase 8) can't
know when to fire.

**Observation worth pinning:** the bug was diagnosed in roughly
two hours of conversation that involved reading source for
`chat.py`, `agent_loop.py`, `router.py`, `capabilities.py`, and
`alias_probe.py`, plus three direct curl tests, plus
ssh-into-container. Phase 7's whole reason for existing is to
turn that two-hour debugging session into a 30-second
dashboard glance plus a `/model` command. The work is
load-bearing for the project's "I'm a programmer, I want to see
what goes wrong" posture (project lead, 2026-05-22).

---

## Narration shape-check fired on every chit-chat turn

**First observed:** 2026-05-12. **Rolled back:** 2026-05-12.
**Tag:** design (closed by removal), sibling of the claim-check
rollback landed the same day.

`is_tool_use_expected_but_none` is a shape-level classifier:
tools were offered + clean finish + no `tool_calls` + reply
over 40 chars → "model declined to call a tool when one was
expected." Shipped 2026-05-11 as a runtime signal emitted by
`record_narrated_tool_call` from the chat tool-loop and cron
firings. The doc
`docs/hallucinations-and-poisoning.md` framed the original
signal with the precondition *"the user's original message
triggered tool-calling expectations"*; the implementation
dropped that precondition because no cheap honest signal
exists for user intent.

Live Telegram session 2026-05-12 produced three
`tool_call_narrated` events in one short conversation:

- `"I'm ready to help! Could you clarify..."` (no prior
  context)
- `"You're welcome! Let me know..."` (reply to "Thanks")
- `"I'm FITT, your personal AI assistant..."` (reply to
  "Who are you")

All three were correct model behaviour. None of them
involved a user asking for an action. The signal fired at
100% on ordinary chit-chat because the Telegram bot
always loads FITT's tool registry into the request, which
the shape check reads as "tools were offered."

**Cost:** The same as claim_check: noisy events train the
operator to ignore the signal, which was meant to surface
genuine tool-call failures. Every Telegram conversation
with casual messages was a false-positive generator.

**Root cause (lesson):** The doc's precondition was the
load-bearing part. Shipping a shape signal without it was
the same anti-pattern as shipping a regex for hallucination
detection: trying to infer user intent on the cheap.

**Fix:** Removed `record_narrated_tool_call` from
`agent_loop.py`, the `detect_narrated_tool_call` detector +
`NarratedToolCall` dataclass + `_NARRATED_TOOL_RE` regex from
`capabilities.py`, the callers in `chat.py` and
`cron_runner.py`, the `tool_call_narrated` event kind from
the CLI color map, the e2e lifecycle test, and the narration
assertions from `test_cron_runner.py`. Every doc / spec /
roadmap reference to the runtime event kind updated.

**What's still real:** `is_tool_use_expected_but_none` stays
as a pure classifier used by `gateway.alias_probe` (boot-time
canary) and `gateway.alias_eval` (on-demand harness). Those
two contexts supply the expected-outcome precondition by
construction — the test author wrote the case. That's where
the signal belongs.

**Rule for future signals:** base decisions on ground truth,
not on flimsy inference of intent. If the cheap signal
requires regex on content, keyword heuristics, or the shape
of the model's reply to decide whether the user wanted an
action, don't ship it in live chat. Put it in the eval
harness where the precondition is pinned, or don't ship it
at all.

---

## Receipt cross-check regex captured "a" as a tool name

**First observed:** 2026-05-12. **Rolled back:** 2026-05-12.
**Tag:** bug (closed by removal), Principle-3 / own-doc
violation.

The `claim_check.py` module shipped 2026-05-11 as
"minimum-viable receipt cross-checking" for Problem C. The
live Telegram session on 2026-05-12 surfaced exactly the
failure mode `docs/hallucinations-and-poisoning.md` had
explicitly warned against: the regex captured `"a"` as a
tool name from the chatty phrase *"using a secure,
privacy-first toolset"*, firing a `tool_claim_mismatch`
event on benign natural-language text.

**Cost:** Every chatty Telegram reply that mentions
"using", "via", or "I used" in passing was a false-positive
candidate. One event per session-with-prose was typical.
More insidious than the event noise: the signal trained the
operator to ignore `tool_claim_mismatch`, which was meant to
flag the actual Problem C failure mode.

**Root cause:** I latched onto the word "receipt" in the
hallucinations doc's item 3 and shipped a regex claim
parser despite the same doc's explicit not-list ("regex
matching on any specific hallucination shape") pointing
straight at it. The "lexical-signal version" framing in the
commit message was wallpaper, not a rationale.

**Fix:** Removed `gateway/src/gateway/claim_check.py`,
`gateway/tests/test_claim_check.py`,
`agent_loop.py::record_claim_mismatch`, the chat + cron
callers, the `tool_claim_mismatch` event kind, and every
doc / spec / roadmap reference. The audit log at
`$FITT_HOME/audit.jsonl` remains the real receipt layer —
tamper-evident and authoritative. An operator checking
`fitt inbox` / `fitt audit tail` when something feels off
is the only reliable cross-check we have. There is no
Phase 2 in the queue; an "LLM-based claim extractor"
parses the same prose the regex did, just more
expensively, so it's the same anti-pattern. When the
user-facing experience of Problem C hurts enough to
revisit, the right conversation is with fresh eyes, not
a plan stashed here.

**Lesson for the agent:** if the backing doc lists the
approach you're about to take on its explicit don't-do
list, the right answer is to not do it, not to rebrand it
as a minimum-viable starter. The doc exists to prevent
exactly this failure mode.

---

## 🔓 Trust session button did nothing

**First observed:** 2026-05-11. **Fixed:** 2026-05-11.
**Tag:** bug (closed), high pain. Principle 8 gap.

Every Telegram approval prompt rendered three buttons:
✅ Approve, ❌ Reject, 🔓 Trust session. Tapping 🔓 correctly
routed the click to the gateway's decide endpoint, which
called `ApprovalMiddleware.trust_session(session_key,
tool_name)` as designed. The method body was a documented
no-op: a single `_log.debug("approval.trust_session.noop",
...)` and nothing else — Task 8c, deferred at Phase 4 shipping
time and never completed. The next tool call in the same
session re-prompted identically to how Approve would have
behaved.

**Cost:** Every multi-step Telegram coding session paid
N taps for N tool calls. Observed during live use on
2026-05-11: three `edit_file` prompts for one turn's work,
with the operator tapping "🔓 Trust session" on the first
one and being confused when the second still asked. A
classic Principle 8 gap — the UI promised session-level
trust, the backend silently didn't deliver.

**Fix:** `ApprovalMiddleware` gained
`_trusted: dict[str, set[str]]` (session_key → trusted tool
names). `trust_session()` writes to it. `check()` gained a
short-circuit after the deny-list check and the early
auto/block/yolo branches: if the session already trusts the
tool, return `ApprovalDecision.trust_session(detail=
"previously trusted for this session")` without creating a
pending approval. `clear_session()` drops the session's
trust set so CLI archive / delete paths stay clean. Trust
is per-(session, tool); it does NOT bypass the deny list
(which runs first); it does NOT survive a gateway restart
(persistent trust graduates to config.yaml's
`bucket=auto`). 8 tests cover the short-circuit path,
cross-session isolation, deny-list precedence, per-tool
scope, restart behaviour, `clear_session`, and the
end-to-end decide-handler flow the Telegram bot uses.

---

## FITT capability block leaks into coding-agent clients (Aider)

**First observed:** 2026-05-11. **Fixed:** 2026-05-11.
**Tag:** design (closed), medium pain. Cross-references the
Phase 4 "tool forwarding, not replacement" decision and the
prompt-injection concerns in Phase 4.7's threat model.

Pointed Aider at FITT as its model backend. Aider's own
system prompt asked something shaped like "what tools do you
have?" FITT answered with its own capability block — the
gateway-side `list_capabilities` / inline tool descriptions
— not with what Aider actually has. The inside-Aider session
then spent its first turn calling `list_capabilities`, got
FITT's tools back, and tried to reconcile two completely
separate agent frameworks in one conversation.

This is the Mode 1 / Mode 2 collision in the open. FITT wants
to be a hub that layers memory + tools + approvals on top of
the model (the Telegram case). Aider is itself a coding agent
that owns its own loop, prompt, tools, diff workflow, and
commit discipline. When Aider treats FITT as "just an
OpenAI-compatible endpoint," any FITT-side injection —
capability block in the system prompt, FITT tools merged
into the request's `tools` array, memory snippets prepended
— actively confuses Aider's own agent.

**Cost:** Proportional to how much the author wants to use
FITT-as-router for coding-agent tools (Aider today; Claude
Code, Cursor, Continue-Agent, Codex, Kiro-CLI tomorrow). At
minimum: one wasted turn per session chasing a ghost tool
list. Worst case: the model pattern-matches on FITT's `ssh`-
routed file tools and tries to call them instead of Aider's
own file edits, which silently breaks the Aider workflow.

**Fix plan:** Router-mode for coding-agent clients. Classify
clients via `X-FITT-Client` (values `aider`, `claude-code`,
`cursor`, `codex`, or the generic `coding-agent`). When the
client is in router mode: skip capability-block injection,
skip FITT tool merge into the `tools` array, skip memory
injection, skip approval middleware (the client owns that
surface). Keep: alias resolution, backend dispatch, cost
tracking, and audit-log entry for model usage. Preserve
today's "agent mode" for Telegram / Open WebUI / raw curl
where FITT's layered value is exactly what's wanted.

Default for unclassified clients stays "agent mode" — safer
toward visibility than silently stripping everything.

Work sits in `gateway/src/gateway/chat.py` at `_inject_memory`,
`_inject_fitt_tools`, and the capability-block check around
line 770. One mode-enum, three gates. Tests prove router-mode
requests pass through cleanly.

This is the concrete answer to the "how much does the coding
framework interfere when FITT is used in an IDE or CLI" open
question. Router mode for known coding agents; agent mode
for everything else.

**Fix landed 2026-05-11:** A new `coding-agent` client tag joins
`{ide, telegram, webui, cli}` as an accepted value for
`X-FITT-Client` and the `client:` field on tokens. Single
source of truth lives at `gateway.auth.is_router_mode_client()`;
`chat.py`'s chat handler calls it once at request entry and
branches:

* Router mode (`coding-agent`): skip memory load, skip
  capability-block construction, skip `_inject_memory`, skip
  `_inject_fitt_tools`, and skip the FITT tool loop
  altogether. The request body reaches LiteLLM as the client
  sent it (minus the client's concrete `model` field, replaced
  by the alias's backend model id — that's the whole point).
  Approval middleware isn't consulted because no FITT tool
  runs.
* Agent mode (everything else — today's behaviour): unchanged.

What FITT still does for router-mode clients: alias resolution
(`fitt-smart` → the configured backend), dispatch via
LiteLLM, cost tracking, audit-log entry for the model call,
fallback handling, `X-FITT-Backend` header. What the client's
own agent owns: system prompt, tool schemas, tool execution,
approval UX, memory.

Default for unclassified clients stays `webui` (from the auth
middleware's token resolution), which is NOT router mode —
safer toward visibility than silently stripping every FITT
feature for a client that hasn't opted in. 9 tests pin the
no-system-message, no-FITT-tools-merged, no-memory-leak,
no-FITT-tool-loop, still-resolves-aliases,
still-rejects-concrete-model-ids contract, plus the Telegram
regression guard and the unclassified-client default.

Operator setup for Aider: add
`X-FITT-Client: coding-agent` to Aider's `extra_headers` config,
or tag the Aider token with `client: coding-agent` in
`secrets.yaml`.

---

## Silent failure when api_keys entry is missing for an openai-backend model

**First observed:** 2026-05-11.
**Partially fixed:** 2026-05-11 (boot-time ERROR log; the
LiteLLM runtime failure is unchanged).
**Tag:** design, Principle 11 (closed).

Adding a new `openai`-backend model (e.g. a new NVIDIA NIM
binding) requires two coordinated edits: `config.yaml` gets
the `models:` entry + alias pointer, and `secrets.yaml` gets
an `api_keys.<model.id>` entry. If the `api_keys` entry is
missing or keyed on the wrong name, the gateway starts
cleanly with no warning. The first time the alias is
dispatched, LiteLLM's router can't find an api_key, falls
back to its default OpenAI client, and raises
`litellm.AuthenticationError: the api_key client option
must be set either by passing api_key to the client or by
setting OPENAI_API_KEY env variable`.

The error message is correct but misleading: the fix isn't
to set `OPENAI_API_KEY`, it's to add the matching
`api_keys` entry in `secrets.yaml`. An operator seeing
this for the first time will reasonably try the obvious
thing and end up confused.

**Cost:** Low in absolute terms (minutes of confusion per
incident) but it's a Principle 11 violation — the
misconfiguration is detectable at boot and we're not
surfacing it. Every new model binding is a fresh
opportunity to hit it.

**Related gotcha worth naming:** `api_keys` is keyed on
the model's `id` field, not on the alias name. Several
aliases can point at the same model id and share a key.
Easy to assume otherwise when staring at `aliases:` and
`api_keys:` side by side.

**Fix plan:** Add a boot-time pass in config load (likely
`config.py` or `app.py` startup) that walks every model
with `backend: openai`, verifies `secrets.api_keys.<id>`
exists, and logs an ERROR with the exact
`api_keys` entry to add when it doesn't. Don't refuse to
start — other aliases might still work — but make the
misconfiguration unmissable in the logs.

Shape:

```
ERROR config.secrets.missing_api_key
  model_id=nvidia-qwen3-coder
  fix="add `api_keys: { nvidia-qwen3-coder: nvapi-... }` to secrets.yaml"
```

Worth bundling with the second Principle 11 item: a
boot-time tool-call reliability probe per alias (in the
hallucinations doc's action list). Both have the same
detect-at-boot-warn-loudly shape. If we do one we should
consider doing the other in the same session.

Hours of work. Not blocking but shouldn't sit forever.

**Fix landed 2026-05-11:** `gateway/src/gateway/config.py`
gained `check_missing_api_keys(config)` which returns a
list of human-readable warnings for openai-backend models
whose `api_keys` entry is missing. `app.py`'s `create_app`
calls it at startup and emits an ERROR log line per
warning. Non-fatal — other aliases still work. Tests in
`test_config_boot_checks.py` cover happy path, missing key,
key-name-mismatch (the exact mistake in the incident),
mixed backends, multiple gaps, and the secrets-not-loaded
CLI case.

The runtime LiteLLM failure with its misleading
"OPENAI_API_KEY not set" message is unchanged — we can't
intercept that without a much bigger middleware
intervention — but now the operator sees the real cause
in the gateway logs at startup before the misleading
runtime error lands. That's the Principle 11 property we
wanted.

The sibling Principle 11 item — boot-time tool-call
reliability probe per alias — is deferred. It needs real
LLM dispatch at startup (network, token cost, timeout
handling) and is bigger than this half-day item.

**Fix landed 2026-05-11 (both halves of Principle 11
backlog).** The tool-call reliability probe shipped as
`gateway/src/gateway/alias_probe.py`: a canary request per
alias at startup with a synthetic `_fitt_probe` tool in the
`tools` array, shape-level classification of the response,
one ERROR log per narrated / truncated / transport-failed
alias. Would have caught the 2026-05-07 qwen2.5-coder
narration and the 2026-05-10 qwen3-next sentinel pattern on
the first gateway boot instead of on the first live
Telegram turn. Sized to the same half-day bucket as the
api_keys check thanks to the `extract_tool_calls` helper
from `agent_loop.py` being reusable. Disabled via
`server.boot_probe_enabled = false` in tests; 10s default
timeout configurable via `server.boot_probe_timeout_s`.

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

**Update 2026-06-03 — recurred (Roland Garros), then fixed.**
Same shape, different domain: asked `fitt-hermes` for "today's
Roland Garros match results"; it refused with "my capabilities
don't allow direct access to real-time data" despite
`web_search` being live. Pointing at the tool explicitly made
it search (so the wiring is fine; it's the proactive judgment
that fails). Two-part fix landed:

1. **Made it measurable.** New `live_fact_web_search` case in
   the eval *realistic* suite (`realistic_cases()`): a
   time-varying question with `web_search` offered, expecting
   the call. A refusal scores `narrated` → red verdict on the
   per-alias page. Kept out of the bare default suite on
   purpose — the prompt-sensitive case belongs only in the
   suite that runs under FITT's live prompt, so the before/
   after is a clean A/B.
2. **Prompt nudge (always-on).** New `[Using tools for current
   facts]` section in the capability-block trailer
   (`capabilities.py`), borrowing the enumerate-the-must-use-a-
   tool-categories shape that **both** Hermes
   (`OPENAI_MODEL_EXECUTION_GUIDANCE` `<mandatory_tool_use>`:
   "Current facts (weather, news, versions) → use web_search")
   and OpenClaw (execution-bias "mutable facts need live
   checks") independently landed on. Names web_search for live
   facts, reframes "you are not limited to training data when a
   tool can fetch the answer", and adds Hermes' retry-on-thin-
   results line to fight the link-dump-instead-of-answer
   symptom.

Caveat (unchanged): prompting reduces the rate, doesn't
eliminate it on an undersized model. Model choice is the real
lever — Hermes' own enforcement list (`gpt`, `gemini`, `qwen`,
`deepseek`, ...) notably excludes the Hermes model family,
implying it tool-calls well natively; the families FITT runs
locally are exactly the ones that need the steering. Validate
per-binding with the realistic eval before trusting it.

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

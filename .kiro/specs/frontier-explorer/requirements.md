# Requirements: Frontier Explorer

**Status:** drafted 2026-08-12, not started. This is the north star the
judged-e2e and coverage work has been serving; recording it so the
subordinate pieces stay subordinate.

## The idea

A frontier agent *interacts with* FITT to find issues — it drives the
conversation, decides what to try next, chases anything that looks wrong,
and reports findings.

## What the judge already is (correcting an earlier overstatement)

An earlier draft of this spec described the judge as grading "fixed
transcripts". That undersold it, and the distinction matters for deciding
what to build. The judge already receives:

* **Tier 1** — every tool call with arguments and results, `loop_status`,
  and the side-effect snapshot (cron jobs, todos, recent events).
* **Tier 2** — the per-iteration timeline (each LLM call with
  `finish_reason`, tokens, tool-call counts) and a *required* root-cause
  hypothesis citing which iteration misbehaved.
* **Tier 3** — the verbatim conversation FITT sent the model each
  iteration.
* An audit ask naming the harness itself as a suspect, plus a
  known-issues checklist.

It has genuine wins on that basis: Tier 2 correctly characterised the
gemma4 spiral ("re-emits the same call every iteration, never produces
`finish_reason=stop`"), caught a loop-brake circumvention, and flagged a
`news_summary` reply as generic even though it passed objectively.

## The real failure mechanism: anchoring, not blindness

`build_judge_prompt` hands the judge the harness's own conclusion:

```
## Objective outcome (deterministic, checked by code)
FAIL — memory_search did not fire on the recall turn
```

…and labels the snapshot `GROUND TRUTH`. So on the five occasions the
*harness* was wrong (unregistered tool, lesson leak across a session,
lesson leak across scenarios, wrong cron delivery channel, dropped
`tool_calls`), the judge was handed a wrong answer presented as
authoritative — and reasoned its way to "the assistant hallucinated a
1-in-10,000 number" rather than doubting what we'd called fact.

Two consequences, in cost order:

1. **Cheap, do first: blind judging.** Same internals, no objective
   verdict, no "ground truth" framing — and replay it against the five
   known cases. If a blind judge catches what an anchored one
   rubber-stamped, most of the discovery value arrives for the price of a
   prompt change.
2. **Still missing, and the reason for this spec: the judge cannot
   ACT.** It reads one run. It cannot form a hypothesis, change one
   variable, and run the experiment — which is how every real defect in
   these sessions was actually found. That's the gap an explorer fills,
   and it stands regardless of how well blind judging works.

## Why, from evidence

Every real defect found in the 2026-08-10..12 sessions came from
*investigation*, not from grading:

| Defect | Found by |
|---|---|
| litellm `ollama_chat` dropping assistant `tool_calls` | wire capture + reading provider source |
| `num_ctx` 4096 starving a ~4095-token prompt | noticing `output_tokens=1` and checking `/api/ps` |
| `memory_search` never registered | reading the registry gate after a suspicious verdict |
| lesson leaking a fact across sessions, then across scenarios | reading the run home's `lessons.md` |
| cron session running on the wrong alias | reading the `cron_failed` event body |
| cron delivery channel is `cron_completed`, not `agent_message` | reading the pusher's skip list |
| `glob_search` hitting Windows `FIND.EXE` | calling the tool directly with valid args |
| cp1252 crash masking a successful run *and* the exit code | running with stdout redirected |

The judge, over the same period, **agreed with a wrong objective verdict
five times**, including once while the contradicting evidence was in its
own prompt (Tier 3 showed the dropped `tool_calls` verbatim and it still
blamed the model). Per the section above, the likeliest cause is
anchoring — we tell it the answer and call it authoritative — not lack of
visibility.

Conclusion: the judge is a decent *diagnostician of one run* and, while
anchored, an unreliable *auditor of the harness*. Discovery needs
something that can also act: form a hypothesis, change one variable, run
it again. Try un-anchoring the judge first (it's a prompt change), then
build the explorer for the part a single-run reader can never do.

## User stories

### 1. It drives, not grades

**1.1** The explorer sends turns to FITT and chooses each next turn based
on what came back — including deliberately awkward ones (contradictions,
ambiguity, missing prerequisites, tools that don't exist).

**1.2** It can read the same internals a human debugger reads: the turn
event timeline, the event log, tool calls with arguments and results, the
side-effect snapshot, and the run home on disk.

**1.3** It reports a finding as a claim plus the evidence that supports
it, so a human can verify without re-deriving.

### 2. It cannot be trusted, so it must be checkable

**2.1** A finding MUST carry the evidence: which turn, which tool call,
which file, which log line. A confident claim with no evidence is noise —
this session produced several.

**2.2** The explorer MUST NOT be the sole judge of whether its finding is
real. Its output is a *candidate* defect, triaged by a human or by a
deterministic reproduction.

**2.3** Findings SHOULD be deduplicated against `docs/observed-issues.md`
and the spec task lists, so a known issue isn't re-reported as news.

### 3. Findings ratchet into regressions

**3.1** A confirmed finding SHOULD become a deterministic artefact — a
contract check or a scenario — so it can never regress silently. That's
the loop: explorer finds, human confirms, suite remembers.

**3.2** The explorer SHOULD be told what's already covered, so it spends
its budget on unexplored surface rather than re-treading the 9 scenarios.

### 4. Safety

**4.1** Runs against an isolated FITT home, like the judged harness, so
exploration can't touch real todos, crons, memory, or the operator's git
repos.

**4.2** Bounded: a turn budget and a wall-clock cap, because an explorer
with a frontier model behind it costs real money and will happily keep
going.

**4.3** MUST NOT be given write-side tools against real projects.

## Non-goals

- Replacing the scenario suite. That becomes the regression floor.
- Autonomous fixing. Finding and fixing are different trust levels; the
  self-improving-loop note in BACKLOG already separates them.
- Grading model quality. That's what the judged standing view is for.

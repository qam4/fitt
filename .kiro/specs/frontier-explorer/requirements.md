# Requirements: Frontier Explorer

**Status:** drafted 2026-08-12, not started. This is the north star the
judged-e2e and coverage work has been serving; recording it so the
subordinate pieces stay subordinate.

## The idea

A frontier agent *interacts with* FITT to find issues — it drives the
conversation, decides what to try next, chases anything that looks wrong,
and reports findings. Not a grader of fixed transcripts: an explorer.

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
own prompt. It inherits the harness's framing, so it cannot be the thing
that questions the harness.

Conclusion: scripted scenarios + a passive judge is a good **regression**
layer and a poor **discovery** layer. Discovery needs an agent that can
form a hypothesis, run an experiment, and read internals.

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

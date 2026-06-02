# Design: FITT Phase 7.6 — Probe Clarity

## Overview

The boot/re-probe and the eval harness both answer "is this
alias healthy and does it tool-call?", but they collapse every
failure into a single `transport_error` status. That label is
misleading: it reads like "can't reach the host" when, on a
single-GPU local Ollama setup, the actual cause is almost
always "the host is fine, the model is cold-loading or queuing
for VRAM." The 2026-05-28 incident that motivated this spec:
re-probing three aliases (qwen3:14b, hermes3:8b, granite3.3:8b)
that all point at one laptop's Ollama on a 12GB GPU returned
"1 of 3 ok, 2 transport_error" — not because two models were
broken, but because the three probes fired concurrently and
fought over VRAM, and two blew past the 10s timeout while
cold-loading.

This phase makes the probe (and eval) tell the truth, across
three axes:

1. **Honest failure vocabulary.** Stop flattening timeouts,
   connection failures, rate limits, and auth errors into one
   word. Adopt the dispatch-outcome taxonomy the chat path
   already uses (`upstream_silent`, `upstream_server_error`,
   etc.) so one word means one thing across the whole project.

2. **Reachability-on-timeout.** When a canary times out, run
   the cheap reachability check `/ready` already does
   (`GET /api/tags` for Ollama) to split "reachable but slow /
   cold-loading" (`upstream_silent`) from "genuinely
   unreachable" (`unreachable`). Directly answers the operator
   question: "is it can't-ping, or slow-inference?"

3. **Sequential same-endpoint probing.** Stop the self-inflicted
   timeouts: aliases that share an endpoint are probed one at a
   time so each model gets the GPU to itself. Plus a per-alias
   probe trigger as the manual companion.

And it makes the dashboard surface this richness without
turning the aliases table into a wall of columns: a **unified
per-alias page** (absorbing the existing eval detail view) is
the rich drill-down; the table stays a lean index.

## Why this is a follow-up to Phase 7, not part of it

Phase 7 (Visibility & Traceability) shipped DONE 2026-05-28.
It built the surfaces (dashboard, per-turn capture, Telegram
commands, eval detail view). This phase fixes a *correctness*
problem in one of those surfaces — the probe's failure
classification — surfaced by living with Phase 7 for all of
a day. Per Principle 9, real use drove it. It's small and
self-contained enough to be its own spec rather than
re-opening a closed phase.

## Background: the three pre-existing mechanisms

FITT already has three separate "is the backend OK" mechanisms
that don't share vocabulary. This phase reconciles them rather
than adding a fourth.

| Mechanism | Question | How | Cost |
|---|---|---|---|
| `/ready` (`health.py`, Phase 1) | Is the backend *reachable*? | `GET /api/tags` (Ollama) / `/v1/models` (cloud) — no inference | ~2.5s, cheap |
| `alias_probe` (Phase 7.1) | Does it emit `tool_calls`? | one canary inference, `stream=False` | full generation |
| `alias_eval` (Phase 4.11) | Does it tool-call across a suite? | 5 canary inferences | 5× generation |
| chat path `_classify_upstream_error` (Phase 4.9) | Why did a *real turn* fail? | the live request | the canonical failure taxonomy |

The chat path has the mature failure taxonomy; the probe/eval
have richer success-shape categories (`narrated`, `truncated`)
but an impoverished failure side. `/ready` has the reachability
ping nobody else consults. This phase wires them together.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Shared dispatch-outcome taxonomy (NEW shared module)       │
│  gateway/dispatch_outcome.py                                │
│                                                             │
│  classify_dispatch_exception(exc) -> DispatchOutcome        │
│    upstream_silent | upstream_rate_limited |                │
│    upstream_client_error | upstream_server_error |          │
│    unreachable                                              │
│                                                             │
│  (chat.py's _classify_upstream_error logic moves here;      │
│   chat.py becomes a consumer)                               │
└─────────────────────────────────────────────────────────────┘
              ▲                    ▲                  ▲
              │                    │                  │
      ┌───────┴──────┐    ┌────────┴───────┐   ┌──────┴───────┐
      │  chat path   │    │  alias_probe   │   │  alias_eval  │
      │ (consumer,   │    │  (failure side │   │ (failure side│
      │  unchanged   │    │   adopts; adds │   │  adopts)     │
      │  behavior)   │    │   reachability │   │              │
      │              │    │   on timeout + │   │              │
      │              │    │   latency)     │   │              │
      └──────────────┘    └────────┬───────┘   └──────────────┘
                                   │
                          ┌────────┴─────────┐
                          │  reachability    │
                          │  check on timeout│
                          │  (reuse /ready's │
                          │  _probe_model)   │
                          └──────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  Dashboard                                                  │
│                                                             │
│  /dashboard/aliases  — lean table: pip + compact probe      │
│                        summary + eval badge + endpoint col  │
│                                                             │
│  /dashboard/alias/<id> — UNIFIED per-alias page (NEW):      │
│      Config · Endpoint + "shares with X,Y" ·                │
│      Probe (4 dims + latency + reachability) ·              │
│      Eval (3 suites, absorbs F18 /dashboard/eval/<id>) ·    │
│      Context window · Recent dispatches                     │
│      Actions: [run eval ▾] [re-probe this alias]            │
└─────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Decision 1: Shared dispatch-outcome taxonomy in one module

The chat path's `_classify_upstream_error` (chat.py) is the
canonical classifier — it already maps LiteLLM/httpx
exceptions to `upstream_silent` / `upstream_rate_limited` /
`upstream_client_error` / `upstream_server_error` with
`error_class` + `error_detail`. Extract it to a new shared
module `gateway/dispatch_outcome.py`; chat.py imports it (no
behavior change); probe and eval adopt it for their failure
side.

**Why a shared module, not duplicate the enum in the probe:**
duplicating would create a fourth inconsistent vocabulary.
The whole problem this phase fixes is vocabulary fragmentation;
the fix can't be "add more fragments." One word, one meaning,
everywhere.

**Vocabulary: keep `upstream_silent`, don't introduce
`timeout`.** The chat path already says `upstream_silent` for a
timeout, and the Telegram bot already branches on it. A probe
that says `upstream_silent` for the same condition means an
operator who's seen the term in a `/lastturn` reply recognizes
it on the dashboard. Consistency across surfaces beats a
locally-prettier word. The dashboard renders it in plain
language ("slow / loading") for the human; the *status token*
stays `upstream_silent`.

**New status `unreachable`** added to the taxonomy for the
genuine can't-connect case (split out of today's
`upstream_server_error` catch-all when the reachability check
confirms the host is down). The chat path doesn't currently
distinguish this (it lumps connection failures into
`upstream_server_error`), but the probe can, because it runs
the reachability check on timeout. The taxonomy gains the
status; the chat path simply never emits it (no behavior
change there).

### Decision 2: Reachability-on-timeout, reusing `/ready`'s ping

When a probe (or eval) canary times out, the bare fact "timed
out" is ambiguous: the host could be cold-loading a model
(reachable, slow) or genuinely down (unreachable). To
disambiguate, on timeout the probe runs the same cheap
reachability check `/ready` already does — `GET /api/tags` for
Ollama, `GET /v1/models` for cloud — and classifies:

- reachable → `upstream_silent` (slow / cold-loading / queuing)
- not reachable → `unreachable` (genuine transport failure)

**Why reuse `/ready`'s mechanism, not build TTFT streaming.**
The earlier design idea was to make the probe *stream* its
canary so it could measure time-to-first-token separately from
total generation time, distinguishing "slow to start (cold
load)" from "slow to generate (undersized GPU)." But the
reachability ping already answers the operator's actual
question ("ping vs slow") with code that already exists. TTFT
is a finer distinction we don't need yet. Reuse over reinvent
(Principle 3). TTFT is a documented future-extension.

**Honest caveat.** A timeout doesn't *perfectly* prove
reachability either way: a silently-dropped/firewalled
connection black-holes packets and hangs to the timeout rather
than failing fast, so the reachability ping would also hang and
report unreachable — which is the right answer in that case.
But "reachable" from the ping means "the host answered
/api/tags," which is strong evidence. The dashboard wording
won't over-claim ("reachable — model slow/loading" is the
conclusion; the raw ping latency is in the tooltip).

**Mechanism extraction.** `/ready`'s `_probe_model` is private
and endpoint-local in `health.py`. It needs to be callable from
the probe. Extract it to a small `gateway/reachability.py`
(or into `dispatch_outcome.py`) as a public
`check_reachable(model, timeout_s) -> ReachabilityResult`;
`health.py` imports it (no `/ready` behavior change). Treat
this extraction carefully — `/ready` is load-bearing for Docker
healthchecks.

**Timeout knobs stay separate.** `/ready` uses
`_PROBE_TIMEOUT_S = 2.5` (a fast reachability ping);
`alias_probe` uses `boot_probe_timeout_s = 10` (an inference
budget). They measure different things and should NOT be
unified — a 2.5s ping budget is correct for reachability, a
10s+ budget is correct for cold-load inference. The
reachability-on-timeout check uses the fast 2.5s ping value;
the inference canary keeps its (raised — see Decision 4)
budget. Documented, not accidental.

### Decision 3: Sequential same-endpoint probing

`probe_all_aliases` fires every alias concurrently via
`asyncio.gather`, with a docstring asserting "no contention
across aliases." That's false for the dominant FITT shape:
multiple aliases on one local Ollama, one GPU. Concurrent
probes force the GPU to serialize model loads, and probes
behind the first model time out while waiting for VRAM.

Fix: **group aliases by resolved endpoint; probe endpoints
concurrently, but aliases *within* an endpoint sequentially.**
Two aliases on different endpoints still probe in parallel (no
contention); three aliases on `laptop:11434` probe one after
another (each gets the GPU). This mirrors the eval harness,
which already runs its cases sequentially for exactly this
reason (shared backend quota).

**Cost:** re-probing N aliases on one endpoint takes ~N×
longer (each cold-load is serial). For 3× 14B-class models
that's maybe 30-60s. Acceptable for an on-demand operator
action; the boot probe pays it once per restart. The dashboard
re-probe button takes longer but stops lying.

### Decision 4: Per-alias probe trigger

Eval is already per-alias (`run_eval_suite(alias, ...)`,
`POST /v1/eval/<alias>`). The probe has a per-alias function
(`probe_alias`) but only ever calls it via the batch. Expose
the per-alias capability:

- `POST /v1/probe/<alias>` (mirrors `/v1/eval/<alias>`)
- a "re-probe this alias" button on the unified alias page

**Why:** it's the manual companion to Decision 3's automatic
fix. When debugging one binding, probe just that one — it gets
the GPU to itself, returns a clean result, doesn't disturb the
siblings or wait for a full sweep. The function already exists;
this is "expose what's there," low cost. Completes the
eval/probe symmetry on the alias page.

**Re-probe-all** (the existing F20 button) stays, now running
sequential-per-endpoint (Decision 3).

### Decision 5: Probe records latency

Every `ProbeResult` gains `latency_ms`. For a VRAM-contended
local setup, latency *is* the health signal: `ok 1.2s` vs
`ok 8.9s` is "healthy" vs "barely made the timeout, watch
this." A timeout sits at the budget ceiling. Latency is
surfaced always (not just on concern) — see Decision 7.

### Decision 6: Unified per-alias page absorbs the eval view

Today `/dashboard/eval/<alias>` (F18) is the eval drill-down.
The probe, now multi-dimensional (reachability, liveness,
tool-call discipline, latency), deserves the same drill-down
treatment — but two separate pages (probe page + eval page)
fragments "tell me about this binding." Consolidate into one
`/dashboard/alias/<id>` page with sections:

- **Config** — model, backend, fallback, **endpoint**, and a
  "Shares `<endpoint>` with `<alias>`, `<alias>`" line when
  other aliases resolve to the same endpoint (the shared-GPU
  insight, in context, where the operator is already asking).
- **Probe** — the 4 dimensions, latency, reachability verdict,
  last-probe timestamp, with a "re-probe this alias" button.
- **Eval** — the 3 suites (default / coding / realistic),
  absorbing the current F18 content verbatim, with the
  "run eval ▾" suite picker.
- **Context window** — tokens + source.
- **Recent dispatches** — the 24h count already on the table.

`/dashboard/eval/<alias>` becomes a redirect to
`/dashboard/alias/<id>#eval` (or is removed; the aliases-table
eval badge re-points to the unified page). No data is lost;
the eval rendering moves, it doesn't change.

### Decision 7: Lean aliases table + endpoint column, no endpoints page

The table is an index, not a detail view (the lesson eval got
right and probe got wrong). Each row answers only "healthy?
good? where?" at a glance:

- **pip** — health, with amber/red split (Decision 8)
- alias · model · backend · **endpoint** (NEW column —
  surfaces the previously-invisible primitive)
- context (compact)
- **probe** — compact summary linking to the alias page:
  `✓ 1.2s` / `⏳ slow 10s+` / `✗ unreachable`
- **eval** — existing `N/M` badge, links to alias page
- dispatched 24h

Everything multi-dimensional (the 4 probe dims spelled out,
reachability facts, reply previews) lives on the alias page,
not crammed into a cell. The row gets *simpler* than today's
F19 tooltip-stuffed "Last probe" cell.

**No endpoints page.** A page must *do* something (action
surface) or be a drill-down target. An endpoints view would
only re-slice data already on the rows — that's a column +
the "shares with" line, not a route. Endpoints are a *property
of an alias's model*, not an entity with a lifecycle; nothing
operates on them. The two facts an operator needs — "which
endpoint does this alias hit" (column + alias-page line) and
"which aliases share an endpoint" (alias-page "shares with"
line) — are both answerable in context without a destination
to navigate to. If endpoints ever gain endpoint-level actions
(wake-satellite, per-endpoint timeout, health history), they'd
earn a page then. Future-extension.

### Decision 8: Pip amber/red split

Today the pip is `ok`/`warn`/`error`/`unknown`. The taxonomy
lets it distinguish "broken binding" from "transient/
environmental":

- **green** — `ok`
- **amber** — environmental, not the model's fault:
  `upstream_silent` (slow/cold-loading), `upstream_rate_limited`,
  `skipped_no_api_key`
- **red** — the binding is genuinely wrong: `narrated`,
  `truncated`, `unreachable`, `upstream_client_error` (auth)
- **grey** — `unknown` / not probed

The amber/red distinction is worth one extra color because
"is this my problem or the model's problem" is the exact
question the operator asks. Your laptop's cold-loading models
go amber, not red — they're not broken, they're loading.

## Components and Interfaces

### Component 1: `gateway/dispatch_outcome.py` (NEW)

```python
DispatchStatus = Literal[
    "upstream_silent",        # timed out; reachable (or unknown)
    "upstream_rate_limited",  # 429/529
    "upstream_client_error",  # other 4xx (auth, bad request)
    "upstream_server_error",  # 5xx, transport, catch-all
    "unreachable",            # confirmed can't-connect (probe only)
]

@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    status: DispatchStatus
    error_class: str           # Python exception class name
    error_detail: str          # truncated message
    upstream_status: int | None = None
    retry_after: str | None = None

def classify_dispatch_exception(exc: Exception) -> DispatchOutcome:
    """The canonical classifier. Lifted from chat.py's
    _classify_upstream_error; chat.py imports it."""
```

`chat.py`'s `_classify_upstream_error` is reduced to a thin
adapter (or removed, with call sites importing the new
function). Behavior identical — pinned by the existing
`test_chat_error_logging.py` suite.

### Component 2: `gateway/reachability.py` (NEW) or fold into dispatch_outcome

```python
@dataclass(frozen=True, slots=True)
class ReachabilityResult:
    reachable: bool
    latency_ms: int
    detail: str | None = None

async def check_reachable(
    model: ModelConfig, *, timeout_s: float = 2.5
) -> ReachabilityResult:
    """Cheap, no-inference reachability ping. Lifted from
    health.py's _probe_model. health.py imports it; /ready
    behavior unchanged."""
```

### Component 3: `alias_probe.py` changes

- `ProbeResult` gains `latency_ms: int` and adopts the
  expanded status set for failures (`upstream_silent`,
  `unreachable`, `upstream_rate_limited`, ...) while keeping
  its success-shape statuses (`ok`, `narrated`, `truncated`).
- `probe_alias`: on timeout, call `check_reachable`; classify
  `upstream_silent` (reachable) vs `unreachable` (not). On
  non-timeout dispatch exception, call
  `classify_dispatch_exception`. Record `latency_ms` always.
- `probe_all_aliases`: group by resolved endpoint; probe
  endpoints concurrently, aliases within an endpoint
  sequentially.

### Component 4: `alias_eval.py` changes

- `CaseResult` failure side adopts the shared taxonomy for the
  `transport_error` cases (timeout → `upstream_silent` /
  `unreachable`; other dispatch failures →
  `classify_dispatch_exception`). Success-shape statuses
  (`pass`, `narrated`, `wrong_tool`, `truncated`) unchanged.
- The eval verdict logic (`_eval_verdict` in dashboard views)
  maps the new statuses sensibly (an `unreachable` case is
  "incomplete — couldn't reach", not "risky").

### Component 5: `POST /v1/probe/<alias>` (NEW endpoint)

Mirrors `/v1/eval/<alias>`. Runs `probe_alias` for one alias,
returns the `ProbeResult` as JSON + updates
`app.state.alias_probe_results[alias]`. Bearer-gated.

### Component 6: Dashboard

- `/dashboard/alias/<id>` — new unified page + template.
- `/dashboard/eval/<alias>` — redirect to alias page (or
  removed; badge re-points).
- `_build_aliases_context` — add `endpoint`, compact probe
  summary, amber/red pip logic.
- `_build_alias_page_context` (NEW) — assemble config + probe
  + eval (reuse `_build_eval_context` logic) + context +
  dispatches + "shares with" computation.
- Actions: per-alias re-probe button → `/dashboard/actions/
  reprobe-alias` (one alias); existing reprobe-all stays.

## Data Models

### `DispatchOutcome` — see Component 1.
### `ReachabilityResult` — see Component 2.
### `ProbeResult` (extended)

```python
@dataclass(frozen=True, slots=True)
class ProbeResult:
    alias: str
    status: ProbeStatus      # expanded: + upstream_silent, unreachable, ...
    detail: str
    latency_ms: int = 0      # NEW
    model_used: str | None = None
    finish_reason: str | None = None
    reply_preview: str = ""
    reachable: bool | None = None  # NEW: set when reachability ran
```

## Error Handling

| Failure | Behavior |
|---|---|
| Canary times out, host reachable | `upstream_silent`, amber pip, "slow / loading (Ns)" |
| Canary times out, host unreachable | `unreachable`, red pip, "unreachable — <detail>" |
| Canary 429/529 | `upstream_rate_limited`, amber |
| Canary 401/4xx | `upstream_client_error`, red |
| Reachability ping itself errors | treat as unreachable; record ping detail |
| Per-alias probe of unknown alias | 404, typed envelope (mirror eval) |
| Sequential probe, one endpoint down | that endpoint's aliases fail independently; other endpoints unaffected |

The probe never raises — every failure becomes a classified
`ProbeResult` (existing contract, preserved).

## Correctness Properties

### Property 1: Failure classification is total
*For any* exception from a probe/eval dispatch,
`classify_dispatch_exception` returns exactly one
`DispatchStatus`; no exception escapes unclassified.
**Validates: Requirements 1.1, 1.2**

### Property 2: Timeout disambiguation
*For any* probe timeout, the result is `upstream_silent` iff
the reachability ping succeeded, else `unreachable`. Never bare
`transport_error`.
**Validates: Requirements 2.1, 2.2, 2.3**

### Property 3: Same-endpoint serialization
*For any* set of aliases sharing one resolved endpoint,
`probe_all_aliases` issues their canary requests
non-concurrently (no two in-flight at once for that endpoint);
aliases on distinct endpoints may overlap.
**Validates: Requirements 3.1, 3.2, 3.3**

### Property 4: Chat-path behavior unchanged
*For any* dispatch exception, the extracted
`classify_dispatch_exception` produces the same `error_type` /
`error_class` / `upstream_status` the inline
`_classify_upstream_error` did.
**Validates: Requirements 1.4, 9.1**

### Property 5: /ready behavior unchanged
*For any* alias chain, `/ready`'s response shape and status
code are identical after `_probe_model` is extracted to
`reachability.check_reachable`.
**Validates: Requirements 2.4, 9.2**
**Validates: no regression in Docker healthcheck.**

## Testing Strategy

- **`test_dispatch_outcome.py`** — the classifier: timeout →
  `upstream_silent`, 429 → `rate_limited`, 401 → `client_error`,
  ConnectError → `server_error`/`unreachable`, etc. Property 1, 4.
- **`test_reachability.py`** — `check_reachable` happy/fail/
  timeout per backend; the `/ready` extraction round-trips
  (Property 5).
- **`test_alias_probe.py`** (extend) — timeout+reachable →
  `upstream_silent`; timeout+unreachable → `unreachable`;
  latency recorded; sequential-per-endpoint (Property 2, 3).
  Mock `check_reachable` and the router.
- **`test_alias_eval.py`** (extend) — failure-side taxonomy
  adoption; verdict mapping for `unreachable`.
- **`test_chat_error_logging.py`** (existing) — must stay green
  unchanged (Property 4).
- **`test_health.py`** (existing) — must stay green unchanged
  (Property 5).
- **`test_dashboard_views.py`** (extend) — unified alias page
  renders all sections; eval redirect; endpoint column; amber/
  red pip; "shares with" line; per-alias re-probe action.
- **Property tests** (hypothesis) — Property 1 (total
  classification over random exception types), Property 3
  (random endpoint→alias groupings serialize correctly).

## Security

- `POST /v1/probe/<alias>` bearer-gated like `/v1/eval/<alias>`.
- Reachability ping hits the same endpoints `/ready` already
  hits; no new outbound surface.
- The "shares with" computation reads config only; no secret
  exposure.

## Known Concerns (tracked, not blocking)

- **Reachability ≠ certainty.** A black-holed connection hangs
  both the canary and the ping; we report `unreachable`, which
  is the right answer, but "reachable" is "answered /api/tags,"
  not a guarantee inference will succeed. Wording avoids
  over-claiming.
- **Sequential probing is slower.** N models on one endpoint =
  N serial cold-loads. Accepted; it's the cost of honest
  results. Progress UX deferred (future-extension).
- **`unreachable` only emitted by the probe**, not the chat
  path (which lumps it into `upstream_server_error`). The
  taxonomy has the status; the chat path doesn't use it. Not a
  drift — the probe simply has more information (it ran the
  ping) than the chat path does.

## Future Extensions (explicit non-goals)

- **TTFT measurement.** Stream the canary to separate
  time-to-first-token (cold load) from generation speed
  (undersized GPU). Reachability-on-timeout answers the
  coarser "ping vs slow" question; TTFT is the finer split.
  Build only if the coarse split proves insufficient.
- **Endpoints page.** If endpoints gain endpoint-level actions
  (wake-satellite, per-endpoint timeout override, health
  history), they'd earn a dedicated view. Today they're a
  property, surfaced via column + "shares with" line.
- **Probe progress UX.** A "probing 2/3..." indicator for the
  now-slower sequential re-probe-all. Silent-but-slow for v1.
- **VRAM-aware capacity hints.** "3 models, 38GB total, 12GB
  VRAM — ~1 hot at a time." Requires VRAM detection (rabbit
  hole). The honest grouping + per-alias symptoms convey it
  without the math.
- **Per-endpoint timeout config.** A slow satellite might want
  a longer probe budget than a fast one. Today one global
  `boot_probe_timeout_s`.

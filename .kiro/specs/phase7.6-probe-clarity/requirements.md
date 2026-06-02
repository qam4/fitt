# Requirements Document

FITT Phase 7.6 — Probe Clarity

## Introduction

The alias probe and eval harness collapse every dispatch
failure into a single `transport_error` status, which reads as
"can't reach the host" when the real cause — on a single-GPU
local Ollama setup — is almost always "the model is cold-
loading or queuing for VRAM." Compounding this, the probe fires
all aliases concurrently, so multiple models on one GPU fight
for VRAM and time out. The operator sees "1 of 3 ok, 2
transport_error" and has no way to tell broken from busy.

This phase makes the probe (and eval) classify failures
honestly using the dispatch-outcome taxonomy the chat path
already owns, disambiguate "slow/loading" from "unreachable"
via the reachability ping `/ready` already runs, probe shared-
endpoint aliases sequentially so they stop self-timing-out, and
surface all of it through a unified per-alias dashboard page
without bloating the aliases table.

Derived from the design in `design.md` (design-first workflow).

## Glossary

- **Probe**: the boot-time / on-demand canary that sends one
  tool-call request to an alias and classifies the response.
- **Dispatch outcome**: the classification of why a dispatch
  succeeded or failed (`ok` / `upstream_silent` / `unreachable`
  / `upstream_rate_limited` / `upstream_client_error` /
  `upstream_server_error`).
- **Reachability ping**: the cheap, no-inference check
  (`GET /api/tags` for Ollama, `/v1/models` for cloud) that
  `/ready` already performs.
- **Endpoint**: the backend URL a model resolves to (e.g.
  `http://laptop.tailnet:11434`). Multiple aliases can share
  one.
- **`upstream_silent`**: the dispatch-outcome status for "we
  waited, the host went quiet (timed out) but is reachable" —
  the chat path's existing term for a timeout.

## Requirements

### Requirement 1: Shared dispatch-outcome taxonomy

**User Story:** As FITT's developer, I want one failure
vocabulary shared across the chat path, the probe, and the
eval, so that a given failure word means the same thing on
every surface and `transport_error` stops being a catch-all.

#### Acceptance Criteria

1. WHERE a dispatch exception occurs in the probe or eval, the
   system SHALL classify it using a shared
   `classify_dispatch_exception` function rather than a
   local/duplicated classifier.
2. THE shared taxonomy SHALL define the statuses
   `upstream_silent`, `upstream_rate_limited`,
   `upstream_client_error`, `upstream_server_error`, and
   `unreachable`.
3. THE shared classifier SHALL record the originating Python
   exception class name and a truncated detail string for
   every classified failure.
4. WHEN the chat path classifies a dispatch exception, it SHALL
   use the same shared function, and its emitted `error_type`,
   `error_class`, and `upstream_status` values SHALL be
   identical to those produced before the extraction.
5. THE probe and eval SHALL retain their success-shape statuses
   (`ok` / `narrated` / `truncated`, plus eval's `pass` /
   `wrong_tool` / `no_tool_expected_but_called`) unchanged.

### Requirement 2: Reachability-on-timeout disambiguation

**User Story:** As an operator, when a probe times out, I want
to know whether the host is unreachable or merely slow/loading,
so that I check networking only when it's actually a networking
problem.

#### Acceptance Criteria

1. WHEN a probe canary times out, THE system SHALL run a
   reachability ping against the resolved model's endpoint.
2. IF the reachability ping succeeds, THEN the probe result
   status SHALL be `upstream_silent`.
3. IF the reachability ping fails, THEN the probe result status
   SHALL be `unreachable`.
4. THE reachability check SHALL reuse the mechanism `/ready`
   uses (extracted to a shared `check_reachable` function), and
   `/ready`'s response shape and status code SHALL be unchanged
   after the extraction.
5. THE reachability ping SHALL use the fast reachability
   timeout (≈2.5s), distinct from the inference canary's
   longer budget.
6. THE probe result SHALL record whether the reachability check
   ran and its outcome.

### Requirement 3: Sequential same-endpoint probing

**User Story:** As an operator with multiple models on one GPU,
I want aliases that share an endpoint probed one at a time, so
that they stop timing out by fighting each other for VRAM.

#### Acceptance Criteria

1. WHEN probing all aliases, THE system SHALL group aliases by
   their resolved endpoint.
2. WHERE multiple aliases share one endpoint, THE system SHALL
   issue their canary requests non-concurrently (no two
   in-flight simultaneously for that endpoint).
3. WHERE aliases resolve to distinct endpoints, THE system MAY
   probe those endpoints concurrently.
4. THE boot probe and the dashboard re-probe-all action SHALL
   both use this sequential-per-endpoint behavior.

### Requirement 4: Per-alias probe trigger

**User Story:** As an operator debugging one binding, I want to
re-probe a single alias on demand, so that it gets the GPU to
itself and returns a clean result without disturbing siblings
or waiting for a full sweep.

#### Acceptance Criteria

1. THE system SHALL expose `POST /v1/probe/<alias>` that probes
   one alias and returns its `ProbeResult` as JSON.
2. THE endpoint SHALL be bearer-gated, matching
   `/v1/eval/<alias>`.
3. THE endpoint SHALL update the in-process
   `alias_probe_results` for that alias.
4. WHEN the alias is unknown, THE endpoint SHALL return 404 with
   a typed error envelope.
5. THE unified alias page SHALL provide a "re-probe this alias"
   action that calls this endpoint.

### Requirement 5: Probe records latency

**User Story:** As an operator on a VRAM-contended setup, I want
to see how long each probe took, so that latency drift toward
the timeout ceiling is visible before it becomes a failure.

#### Acceptance Criteria

1. EVERY probe result SHALL record an elapsed `latency_ms`.
2. THE dashboard SHALL surface latency for every alias
   (including healthy ones), not only on failure.

### Requirement 6: Unified per-alias dashboard page

**User Story:** As an operator, I want one page that tells me
everything about a binding — config, probe detail, eval results,
context window, recent use — so that I don't navigate between
fragmented views.

#### Acceptance Criteria

1. THE system SHALL provide `/dashboard/alias/<id>` rendering:
   config (model, backend, fallback, endpoint), probe detail
   (status, latency, reachability, the four dimensions), eval
   (default / coding / realistic suites), context window, and
   recent-dispatch count.
2. THE eval content currently at `/dashboard/eval/<alias>`
   (F18) SHALL be absorbed into this page without loss of
   information; the old route SHALL redirect to the unified
   page (or the aliases-table eval badge SHALL re-point to it).
3. THE page SHALL display the resolved endpoint, and WHERE
   other aliases share that endpoint, a "shares with `<alias>`,
   `<alias>`" line.
4. THE page SHALL provide "run eval" (suite-pickered) and
   "re-probe this alias" actions.

### Requirement 7: Lean aliases table with endpoint visibility

**User Story:** As an operator, I want the aliases table to stay
a scannable index — healthy? good? where? — with rich detail one
click away, so that surfacing more probe data doesn't turn it
into a wall of columns.

#### Acceptance Criteria

1. THE aliases table SHALL show, per row: a health pip, alias,
   model, backend, endpoint, context window (compact), a
   compact probe summary, an eval badge, and 24h dispatch count.
2. THE compact probe summary and eval badge SHALL each link to
   the unified alias page.
3. THE table SHALL NOT inline the full multi-dimensional probe
   detail (that lives on the alias page).
4. THE system SHALL NOT add a separate endpoints page; endpoint
   visibility is the table column plus the alias-page "shares
   with" line.

### Requirement 8: Health pip distinguishes environmental from broken

**User Story:** As an operator, I want the at-a-glance pip to
tell me whether a not-OK alias is my problem (environmental) or
the model's problem (broken binding), so that a cold-loading
model doesn't look alarming.

#### Acceptance Criteria

1. THE pip SHALL be green for `ok`.
2. THE pip SHALL be amber for environmental/transient statuses
   (`upstream_silent`, `upstream_rate_limited`,
   `skipped_no_api_key`).
3. THE pip SHALL be red for binding-broken statuses (`narrated`,
   `truncated`, `unreachable`, `upstream_client_error`).
4. THE pip SHALL be grey for `unknown` / not-probed.

### Requirement 9: No behavioral regression in existing surfaces

**User Story:** As FITT's developer, I want the chat path and
`/ready` to behave identically after the shared extractions, so
that this refactor surfaces information without changing live
behavior.

#### Acceptance Criteria

1. THE existing `test_chat_error_logging.py` suite SHALL pass
   unchanged.
2. THE existing `test_health.py` suite SHALL pass unchanged.
3. THE chat path SHALL NOT emit the `unreachable` status (it
   does not run the reachability ping); `unreachable` is
   probe-only.

# Phase 4.9 — Upstream Timeouts and Honest Surfacing: Requirements

## Context

Yesterday's live testing surfaced a fault that had been
hiding behind the new "gateway.chat.failed" structured logs
since they shipped. Five turns to `fitt-smart` (currently
routed to `nvidia-deepseek-v4-flash`) showed up in the bot
log as `failure_kind=transport`, `error_class=ReadTimeout`.
Three of those five turns succeeded on the gateway side
2-7 minutes after the bot had already shown the user
"⚠️ gateway unreachable" — the gateway paid for the tokens,
logged `status=ok`, and the answer was lost because nothing
was listening any more.

The proximate cause is the bot's hardcoded 120s
`_STREAM_TIMEOUT_S`. The deeper fault is that the system
has no explicit answer for "what happens when the upstream
LLM goes silent for longer than the user is willing to
wait?"

There are seven separate timeouts in the request path
today: the bot's HTTP read-timeout, the gateway's approval
timeout, the gateway's detach threshold (off by default),
LiteLLM's implicit `acompletion` timeout, the upstream
provider's own server-side timeout, the cron firing
timeout, and the SSE heartbeat. They don't agree with each
other — in particular, the bot's 120s read-timeout is
shorter than LiteLLM's ~600s implicit timeout, which is
how a "successful" gateway response can arrive at a
listener that gave up minutes ago.

This spec answers: **what does the system promise the user
when an upstream LLM call exceeds its timeout, and how do
we guarantee that promise?**

The decision documented in design.md is:

* No detach for slow chats. (The existing detach
  mechanism stays, scoped to its original purpose:
  approval pending.)
* The gateway stops awaiting the upstream at a configured
  timeout, returns a structured error to the bot
  immediately, and lets the orphan task continue under
  `asyncio.shield`.
* The bot's read-timeout is documented as needing to be
  strictly longer than the gateway's upstream timeout,
  so the bot is always reading the gateway's structured
  error rather than its own ReadTimeout. v1 sets sane
  defaults (gateway 300s, bot 360s) and documents the
  invariant; boot-time enforcement is deferred.
* The bot's user-facing error messages distinguish the
  four failure shapes (gateway unreachable, upstream
  silent, upstream HTTP error, mid-stream stall).
* The architectural shape is set up so a future reaper
  (operator-facing audit of the orphan's eventual
  outcome) is a single follow-up commit, not a
  refactor. v1 itself does not ship the reaper.

## User stories

### U1. The bot tells me what actually went wrong

As a FITT user on Telegram, when a chat turn fails I
want to see *what* failed (gateway, upstream provider,
network) so I can decide whether to retry, switch alias,
or wait. "Gateway unreachable" must mean the gateway is
unreachable, not "the upstream took too long."

**Acceptance:**

- **1.1** When the gateway is genuinely unreachable
  (DNS, connect refused, TCP reset), the bot shows
  "⚠️ FITT gateway unreachable" with the underlying
  exception class.
- **1.2** When the upstream LLM goes silent past the
  gateway's configured timeout, the bot shows
  "⏱️ Upstream `<provider>` went silent after `<N>`s
  — likely queued. Try again, or switch alias." The
  alias name is the human-readable alias (`fitt-smart`),
  not the backend model id.
- **1.3** When the upstream returns an HTTP error
  status (429, 503 with Retry-After, 504, generic 5xx),
  the bot shows the status and a short hint
  ("rate limited", "overloaded", "upstream error").
- **1.4** When the stream is established and then dies
  mid-flight (`[ERROR]` marker), the bot shows
  "⚠️ Upstream stopped responding mid-reply".
- **1.5** Every user-visible message carries a
  `request_id` short tag (first 8 chars) so the user
  can paste it in a bug report and the operator can
  grep both log files.

### U2. The system never silently completes a turn the user has been told failed

As an operator, I want a guarantee that no chat turn
silently succeeds on the gateway side after the bot has
told the user it failed without me being able to detect
it.

**v1 acceptance (this spec, ~6 hours):**

- **2.1** The chat handler structures the upstream
  dispatch as a shielded `asyncio.Task` so a future
  reaper can attach with one line of code at the
  existing `TimeoutError` site, without refactor.
  (See design.md "v1 scope" — the shield is the
  load-bearing decision; v1 doesn't yet read the
  task's eventual outcome, but the architecture
  supports it.)
- **2.2** Every chat-completion log row carries
  `request_id` (already shipped in
  commit 85e60d2). v1 ensures the new
  `status=upstream_silent` row also carries it.

**Deferred to a follow-up (~3 hours, see design.md
"Future migration paths"):**

- A background reaper that awaits the orphan
  dispatch task to its conclusion and logs one
  `chat.upstream_late_outcome` row with `outcome`,
  `latency_ms`, `input_tokens`, `output_tokens`.
- The reaper survives `asyncio.CancelledError` from
  gateway shutdown.
- `docs/quickstart.md` "Reading the timeout logs"
  section with `jq` recipes.

The reaper is the answer to "did the gateway
silently succeed?" v1 ships without it because the
audit data is operator-facing-only and only valuable
if you keep using FITT. The architectural shape is
already in place to add it later.

### U3. Gateway timeout is shorter than bot read-timeout

As an operator, I want the gateway's upstream timeout
and the bot's HTTP read-timeout to be ordered correctly
so I don't accidentally re-create yesterday's bug.

**v1 acceptance (this spec):**

- **3.1** The gateway exposes
  `models.upstream_timeout_secs` as an explicit
  configuration value, passed verbatim to
  `litellm.acompletion`'s timeout argument. Default
  300s.
- **3.2** The bot's `_STREAM_TIMEOUT_S` raised to
  360s with a comment at the constant explaining
  the invariant.
- **3.3** Both `configs/config.example.yaml` and
  `gateway/README.md` document the invariant in
  prose. Operators reading the configs see what
  the relationship is.
- **3.4** The gateway's structured timeout error
  carries the configured `upstream_timeout_secs`
  value in the response body so the bot's user-
  facing message can include the actual number
  rather than a hardcoded one.

**Deferred to a follow-up (~1 hour):**

- Boot-time validation: gateway echoes
  `X-FITT-Upstream-Timeout` on every response, bot
  fetches it on `GatewayClient` construction,
  raises if its read-timeout isn't strictly
  greater. Skip unless the
  `failure_kind="bot_read_timeout"` warning shows
  up in real use.

### U4. The structured log captures the failure shapes distinctly

As an operator, I want every chat-completion log row
to carry one of a small set of well-defined statuses,
so `grep` / `jq` queries can produce reliable counts of
each failure mode without ambiguous overlap.

**v1 acceptance (this spec):**

- **4.1** New `status=upstream_silent` value emitted
  by the chat handler when the gateway's upstream
  timeout fires before LiteLLM raises any error of
  its own. Mutually exclusive with the existing
  `upstream_rate_limited`, `upstream_client_error`,
  `upstream_server_error`, `no_backend_available`,
  `stream_failure`, `tool_loop_exhausted`, `ok`,
  `stream_started`, `detached`.

**Deferred (with the reaper):**

- New `event=chat.upstream_late_outcome` written
  by the reaper, carrying `request_id`, `outcome`,
  `latency_ms`, `alias`, `model`, `input_tokens`,
  `output_tokens`. Operator-facing only.
- `docs/quickstart.md` "Reading the timeout logs"
  subsection with `jq` recipes for "show me every
  late-success" and "what fraction of slow turns
  eventually succeed".

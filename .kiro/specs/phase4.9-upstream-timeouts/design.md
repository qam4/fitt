# Phase 4.9 — Upstream Timeouts and Honest Surfacing: Design

## Context: yesterday's bug

On 2026-05-13, five chat turns sent through Telegram
showed `⚠️ gateway unreachable` to the user. Tracing the
five `request_id`s through `gateway.log`:

| request_id | bot deadline | gateway final outcome | upstream latency |
|---|---|---|---|
| 7e82874a | 00:08:31 | 504 from NVIDIA | 15 min 8 s |
| 59783c41 | 00:32:35 | succeeded | 7 min 33 s |
| 5f3aff1a | 00:35:57 | succeeded | 2 min 15 s |
| 3b8b86e4 | 00:39:07 | 504 from NVIDIA | 15 min 7 s |
| 05bbe328 | 02:35:30 | succeeded | 5 min 24 s |

Three of the five "failures" were actually successes the
user never saw. The gateway paid for tokens, logged
`status=ok`, and the bot had already disconnected and
displayed an error.

The proximate cause: `_STREAM_TIMEOUT_S = 120.0` in
`telegram-bot/src/fitt_telegram_bot/gateway_client.py`,
shorter than LiteLLM's implicit default of ~600s, so the
bot is structurally guaranteed to give up before the
gateway has a chance to surface its own structured
error.

The deeper cause: there was no design for "what happens
when the upstream goes silent." The bot's 120s, the
gateway's nothing, LiteLLM's 600s, NVIDIA's 900s
(observed) all interact in ways nobody had drawn out.
This spec draws it out.

## The four user-facing timeouts in the request path

| Timeout | Detects | Resulting state | User should see |
|---|---|---|---|
| **A. Approval timeout** | User away from phone, no tap | Tool auto-rejected as `approval_timed_out`, model continues with adapted reply | Model's adapted reply |
| **B. Upstream timeout** | NVIDIA queue depth, OpenRouter overload | Gateway stops awaiting, returns structured error, orphan task continues to reaper | "Upstream went silent — try again, or switch alias" |
| **C. Tool execution timeout** (e.g. `project_shell`) | Stuck subprocess | Subprocess killed, tool returns `timed_out` error, model continues | Model's adapted reply with the tool error in context |
| **D. Gateway unreachable** | Gateway crashed, network partition | Bot couldn't establish or maintain connection | "FITT gateway unreachable" with exception class |

A and C are working correctly today. D is correctly
detected (the bot does see `httpx.ConnectError` etc.) but
its user-facing message ("gateway unreachable") is
overloaded with B because the bot lumps every
`httpx.RequestError` into the same branch. B has no
gateway-side handling at all.

## Why we chose "stop awaiting + reap, do not push late
results"

The decision space, considered:

### Option 1: Detach for slow chats

Threshold fires → gateway returns a placeholder ("⏳ Still
working — I'll update you when this completes") → bot
shows the placeholder → if upstream eventually replies,
gateway pushes a `late_chat_completion` event → bot
delivers as a Telegram message with no inline reply
context.

**Pro**: No information lost. User sees the answer
eventually if it ever arrives.

**Con (decisive)**: Requires a `late_chat_completion`
event kind, a renderer for it on the bot side, and a
contract for what to do when the user has already moved
on to a different topic in the same chat. None of these
exist today. Building them is a real project — possibly
two days, possibly more once the renderer's edge cases
emerge.

The cost is structurally similar to detach for tool
approvals (which we built), but the user-facing
contract is meaningfully harder. With approvals there's
already an inline keyboard tying the late event to a
specific UI action; for chat completions the late
message arrives in a void.

### Option 2: Stop awaiting, return honest error, do not deliver late results

Gateway timeout fires → gateway returns structured error
to bot immediately → bot shows accurate user message →
in-flight LiteLLM call continues under `asyncio.shield`
→ a small reaper task awaits the orphan and writes one
log line when it concludes → reaper does NOT push to
the user.

**Pro**: Simple. The user-side UX matches what every
other agent client does ("I gave up at N seconds").
The reaper gives the operator audit data without
committing to user-facing late delivery.

**Con**: The user has to retry on their own. Tokens
are paid for upstream replies that no human reads.

### Option 3: Cancel the upstream call

Same as Option 2 but try to actually cancel the
in-flight HTTP request when the timeout fires.

**Pro**: Saves tokens (maybe — the GPU has already
started, billing typically locks in at request
acceptance).

**Con (decisive)**: Cancellation correctness is fragile
to verify. We'd be asking LiteLLM to forward an
`asyncio.CancelledError` to httpx to forward a
connection close to NVIDIA. Each layer can swallow,
mistranslate, or partially apply the signal. Even if it
works today it could silently regress on any LiteLLM
upgrade. The token-saving benefit is small (provider
likely bills regardless once the GPU started). Not
worth the verification cost.

**We chose Option 2.** It's the simplest behavior that
keeps the user honest, gives the operator visibility,
and doesn't commit to a complex new feature. If the
audit log shows late outcomes are common AND useful AND
recoverable, we can promote to Option 1 with the data
in hand.

**v1 ships the user-facing half of Option 2** — the
gateway stops awaiting and returns an honest error,
the bot shows a useful message, the orphan task
continues under `asyncio.shield` so a future commit
can attach behavior to it. The audit reaper itself is
deferred; v1 sets up the architectural shape, the
reaper is one short follow-up commit. See the "v1
scope" section below.

## The cancellation invariant

The single load-bearing rule that makes Option 2 work:

> The bot's HTTP read-timeout is **strictly greater than**
> the gateway's upstream timeout, by enough margin that the
> gateway's structured error is always the message the bot
> reads.

If this invariant is violated, we re-create yesterday's
bug. The bot bails before the gateway returns its
structured error, the gateway returns the error to a
listener that's gone, the bot falls through to its own
ReadTimeout path, the user sees "gateway unreachable"
that it isn't.

To make the invariant impossible to violate by
accident:

* The bot reads `gateway.upstream_timeout_secs` from the
  gateway at startup (fresh `/v1/health` or `/v1/models`
  call returns it as a header `X-FITT-Upstream-Timeout`),
  and refuses to start if its own configured read-timeout
  is not strictly greater.
* Documented default: gateway 300s, bot 360s. The 60s
  margin is generous; even a slow-poke gateway with full
  network buffers couldn't realistically take 60s to
  serialize a 1KB JSON error response.

## Architecture: where each piece lives

```
                       ┌─────────────────────────────────┐
                       │ telegram-bot                    │
                       │  gateway_client.chat()          │
                       │  read_timeout = 360s            │
                       │  (must be > gateway upstream)   │
                       └──────────┬──────────────────────┘
                                  │
                                  ▼   POST /v1/chat/completions
                                       X-Request-Id
┌──────────────────────────────────────────────────────────┐
│ gateway                                                  │
│                                                          │
│  RequestIdMiddleware  (commit 85e60d2)                   │
│   │                                                      │
│  AuthMiddleware                                          │
│   │                                                      │
│  chat.chat_completions()                                 │
│   │  start = time.perf_counter()                         │
│   │  request_id = state.request_id                       │
│   │                                                      │
│   ▼                                                      │
│  AliasRouter.dispatch()  ───►  litellm.acompletion(      │
│                                 timeout=upstream_timeout_secs │
│                                )                         │
│                                                          │
│  asyncio.wait_for(asyncio.shield(dispatch_task),         │
│                    timeout=upstream_timeout_secs)        │
│                                                          │
│  ┌── happy path ─────────────────────────────────────┐   │
│  │  dispatch_task completes within timeout           │   │
│  │  → log status=ok                                  │   │
│  │  → return real response                           │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌── timeout path ───────────────────────────────────┐   │
│  │  TimeoutError raised, dispatch_task still running │   │
│  │  → schedule reaper(dispatch_task, request_id, …)  │   │
│  │  → log status=upstream_silent (chat.completion)   │   │
│  │  → return 503 + {error.type: "upstream_silent",   │   │
│  │      timeout_secs, alias, request_id}             │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  reaper (background task)                                │
│   │  awaits dispatch_task to conclusion                  │
│   │  catches Exception, classifies, builds outcome dict  │
│   │  writes one log line: chat.upstream_late_outcome     │
│   │  exits                                               │
└──────────────────────────────────────────────────────────┘
```

## Module changes

### `gateway/src/gateway/config.py`

Add to `models` config block (or wherever the model
config currently lives — see existing
`alias` / `models` shape):

```python
class ModelsConfig(BaseModel):
    upstream_timeout_secs: float = 300.0
    """Per-call timeout passed to ``litellm.acompletion``.
    Read by the chat handler and exposed to clients via
    the ``X-FITT-Upstream-Timeout`` response header on
    every ``/v1/`` response so the bot can validate its
    read-timeout invariant."""
```

The bot reads it from the response header, not by
hardcoded knowledge of the gateway's config.

### `gateway/src/gateway/router.py`

Pass the timeout explicitly to `litellm.acompletion`:

```python
result = await litellm.acompletion(
    **call_kwargs,
    timeout=self._config.models.upstream_timeout_secs,
)
```

LiteLLM's `Timeout` exception is already classified by
the existing `_classify_upstream_error` helper. We add
one new branch for "the timeout we set, not LiteLLM's
internal one" by checking elapsed time vs. configured
timeout — if elapsed < configured, LiteLLM's own
ceiling fired (rare), otherwise it's our timeout.

### `gateway/src/gateway/chat.py`

Wrap the dispatch call in `asyncio.wait_for` with
`asyncio.shield`, mirroring the detach pattern:

```python
dispatch_task = asyncio.create_task(
    alias_router.dispatch(parsed.model, request_body)
)
try:
    dispatch = await asyncio.wait_for(
        asyncio.shield(dispatch_task),
        timeout=cfg.models.upstream_timeout_secs,
    )
except TimeoutError:
    _schedule_reaper(dispatch_task, request_id, parsed.model, ...)
    return _upstream_silent_response(
        timeout_secs=cfg.models.upstream_timeout_secs,
        alias=parsed.model,
        request_id=request_id,
    )
```

The `_upstream_silent_response` returns 503 with body
`{"error": {"type": "upstream_silent", "timeout_secs":
N, "alias": "fitt-smart", "message": "..."}}` plus the
`X-Request-Id` echo header.

For the tool-loop case, the same wrapper applies around
`run_with_detach` (the existing detach mechanism for
approvals stays — they're orthogonal).

### `gateway/src/gateway/upstream_reaper.py` (new)

Tiny module:

```python
async def reap_orphan(
    dispatch_task: asyncio.Task,
    *,
    request_id: str,
    alias: str,
    started_at: float,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Background worker: await the orphan dispatch
    task and write one log line with its outcome."""
    try:
        result = await dispatch_task
        outcome = "ok"
        in_tokens, out_tokens = _extract_usage(result)
    except Exception as exc:
        outcome = _classify_late_outcome(exc)
        in_tokens, out_tokens = 0, 0

    latency_ms = int((time.perf_counter() - started_at) * 1000)
    log.info(
        "chat.upstream_late_outcome",
        request_id=request_id,
        alias=alias,
        outcome=outcome,
        latency_ms=latency_ms,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
    )
```

`_classify_late_outcome` shares the bucket logic with
`_classify_upstream_error` in `chat.py`.

The reaper is scheduled with the same
`_BACKGROUND_WORKERS` strong-reference set pattern that
`detach.py` uses, so the task isn't GC'd mid-run.

### `gateway/src/gateway/health.py` (or middleware)

Add `X-FITT-Upstream-Timeout` response header to every
`/v1/*` response carrying the configured value.
Cleanest spot: a tiny middleware (or extend the
existing `RequestIdMiddleware`).

### `telegram-bot/src/fitt_telegram_bot/gateway_client.py`

Three changes:

1. **Boot-time validation.** On `GatewayClient`
   construction or on first `list_aliases`, fetch the
   gateway's `X-FITT-Upstream-Timeout`. Raise (and the
   bot's `__main__` logs + exits) if the bot's
   configured read-timeout is not strictly greater.

2. **Split the `RequestError` branch in `chat()`.**

   ```python
   except httpx.ReadTimeout as e:
       _log.warning("gateway.chat.failed", ...,
                    failure_kind="bot_read_timeout", ...)
       yield "⏱️ FITT didn't respond in time on the bot " \
             "side. The gateway probably got an answer " \
             "after this — see telegram-bot.log."
   except (httpx.ConnectError, httpx.ConnectTimeout) as e:
       _log.warning(..., failure_kind="connect_failure", ...)
       yield "⚠️ FITT gateway unreachable: " + type(e).__name__
   except httpx.RequestError as e:
       _log.warning(..., failure_kind="transport", ...)
       yield "⚠️ Network error reaching FITT: " + str(e)
   ```

   Note: with the cancellation invariant in place, the
   ReadTimeout branch should be effectively unreachable
   in production — the gateway always answers first.
   Having it raise the alarm explicitly when it does
   fire is the point.

3. **Map `upstream_silent` to its own user message.**

   In `_format_error`, branch on `error.type`:

   ```python
   if error_type == "upstream_silent":
       n = parsed["error"].get("timeout_secs", "?")
       alias = parsed["error"].get("alias", "?")
       yield (f"⏱️ Upstream `{alias}` went silent for {n}s "
              f"— likely queued. Try again, or pick a "
              f"different alias.")
   ```

## Properties to test

1. **`upstream_silent` fires when configured.** Stub
   `litellm.acompletion` to never return; assert the
   chat handler returns 503 `upstream_silent` within
   `timeout_secs + 1`s.
2. **Reaper logs the outcome.** With the same stub, but
   the stub returns a real result after 2*timeout, the
   reaper writes a `chat.upstream_late_outcome` row
   with `outcome=ok` and the right token counts.
3. **Reaper survives `asyncio.CancelledError` from
   gateway shutdown.** Important: the reaper task isn't
   tied to the HTTP request's lifetime; it's tied to
   the dispatch task's. Shutting down the FastAPI app
   should cancel the reaper, and the test pins that
   the reaper logs `outcome=cancelled` rather than
   raising.
4. **Bot's read-timeout invariant.** Test with bot
   read-timeout 100s, gateway upstream 300s, assert
   the bot refuses to construct.
5. **Bot translates `upstream_silent` to the right
   user string.** Existing `test_chat_error_logging.py`
   pattern, new test file
   `test_chat_error_messages.py` covering every error
   shape's user-visible string.
6. **`X-FITT-Upstream-Timeout` header present on
   every `/v1/*` response.** Single test in
   `test_request_id.py` or a new `test_headers.py`.

## v1 scope

Three commits, ~6 hours. v1 establishes the
architectural shape (typed errors, shielded dispatch
task, request_id correlation) and fixes the
user-facing bug ("gateway unreachable" lying), but
intentionally stops short of the audit data and the
late-delivery feature. See tasks.md for the work
items.

The shielded `dispatch_task` in commit 1 is the
load-bearing decision: nothing in v1 attaches to it
after the timeout fires (the orphan is silently
GC'd), but the *moment* a future commit wants to
attach a reaper, canceller, or late-delivery push,
it's a single `asyncio.create_task(reap(task, ...))`
at the existing TimeoutError site. No refactor.

## What v1 does NOT do

* Does not deliver late completions to the user.
  (See Option 1 above; conditions for promoting
  documented below.)
* Does not log a late-outcome audit row. (See
  reaper, below — deferred to a follow-up.)
* Does not cancel the in-flight LiteLLM request.
  (See Option 3 above; the orphan completes
  naturally or is cut by LiteLLM's own internal
  timeout, which becomes the de-facto upper bound
  on the orphan's lifetime.)
* Does not validate the bot/gateway timeout
  invariant at boot. (See header-echo, below —
  deferred. v1 sets sensible defaults and
  documents the invariant; nothing enforces it.)
* Does not change the existing detach-for-
  approvals mechanism. That's a separate, working
  feature for a different problem.
* Does not change cron firing timeouts, tool-
  execution timeouts, approval timeouts. None of
  those interact with this fault.

## Future migration paths

Each of these is a clean extension on top of v1.
Architecture set up by v1 supports all of them
without refactor.

### Reaper for late-outcome audit (~3 hours)

Promote when: you want to know how often
`upstream_silent` turns eventually succeed.

Add `gateway/src/gateway/upstream_reaper.py` (~30
lines mirroring `detach.py`'s
`_BACKGROUND_WORKERS` strong-reference pattern).
At v1's `TimeoutError` site, schedule
`reap_orphan(dispatch_task, request_id, alias,
started_at)`. Reaper awaits the task to
conclusion, classifies via the existing
`_classify_upstream_error` helper, writes one
`chat.upstream_late_outcome` log line with
`outcome` (`ok`, `upstream_error`,
`upstream_status_<code>`, `litellm_timeout`,
`cancelled`), `latency_ms`, `input_tokens`,
`output_tokens`. No user-side delivery.

Tests: stubbed LiteLLM that blocks then resolves /
raises / never returns; pin each outcome shape;
pin reaper handles `asyncio.CancelledError` from
gateway shutdown.

### Bot startup invariant validation (~1 hour)

Promote when: you start seeing
`bot_read_timeout` warnings in
`telegram-bot.log` (i.e. the invariant is being
violated in practice, not theoretically).

Add `X-FITT-Upstream-Timeout: <secs>` response
header to every `/v1/*` response (extend
`RequestIdMiddleware` or add a sibling). On
`GatewayClient` construction, perform one cheap
GET, read the header, raise if the bot's
configured read-timeout is not strictly greater.
Bot's `__main__` catches and exits with a clear
message including both numbers.

### Late delivery to the user — Option 1 (~1 day on top of reaper)

Promote when: reaper data shows >20% of
`upstream_silent` failures eventually succeed
upstream AND those answers are useful enough to
the user that the "received late, no inline
context" UX is preferable to dropping them.

Add `late_chat_completion` and
`late_chat_failed` event kinds. Bot's events
poller renders them with a clear decoration
("Late reply from your earlier turn:"). Reaper,
instead of just logging on success, also emits
the event. Failures stay logged-only (delivering
"by the way that thing you were waiting for
also failed" is worse than silence).

The "user has moved on to a new topic" question
is the design risk. Mitigation: include the user
turn's first 60 chars in the late-event header
("Late reply: 'What can you do?' →") so the user
has context for what the message is responding
to.

### Cancellation of in-flight LiteLLM requests — Option 3 (~half day plus verification)

Promote when: token waste from orphan requests
becomes a measurable cost, and you have a way to
verify that LiteLLM/httpx actually propagate the
cancel cleanly to the upstream provider.

Replace `asyncio.shield(dispatch_task)` with
`dispatch_task.cancel()` at the `TimeoutError`
site (and remove the shield entirely if no
reaper exists; if a reaper exists, change its
contract to "log that the cancel was issued, no
late outcome expected").

The verification is the actual work. Need a
test that monitors the upstream socket and pins
that `dispatch_task.cancel()` results in a TCP
close to the upstream within Ns. Across LiteLLM
versions and across backend types (NVIDIA's
OpenAI-compatible endpoint, OpenRouter, Ollama).
That's enough surface to be its own small
project.

### Per-alias timeout overrides (~30 minutes)

Promote when: one alias's "right" timeout
diverges enough from the global default that it
matters (e.g. local Ollama is reliably <30s,
cloud is occasionally 5min, and a single
`upstream_timeout_secs` is too coarse).

Move `upstream_timeout_secs` from
`models.upstream_timeout_secs` (top-level) to
per-`ModelConfig` with the global as the
default. Router reads it from the candidate's
config when constructing the LiteLLM call.

### Detach for slow chats — promotion to events delivery

Already covered above as "Late delivery." This
section retained for searchability — the v1
spec ruled this OUT explicitly because it
requires the events-channel work and a
non-trivial UX contract for "late reply with no
inline context". Pick this up only when reaper
data justifies it.

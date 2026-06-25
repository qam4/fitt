# Phase 4.9 — Upstream Timeouts and Honest Surfacing: Tasks

**Status:** shipped

## Scope

Three commits, ~6 hours total. Sets up the right
architectural shape (typed errors, shielded dispatch
task, request_id correlation) so the deferred work
(reaper, late delivery, startup validation) can be
added later as ~half-day extensions without
refactoring. See design.md "Future migration paths"
for what each extension costs.

## Commit 1: Gateway returns honest, typed errors

Goal: the gateway stops awaiting the upstream at a
configured timeout, returns a 503 with
`error.type="upstream_silent"`, and structures the
dispatch task so a future reaper / canceller / late
delivery can attach without refactor.

- [ ] 1a. Add `models.upstream_timeout_secs: float =
        300.0` to `gateway/src/gateway/config.py`
        (positive-float validator). Document in
        `configs/config.example.yaml` with a comment
        explaining the bot-side invariant.
- [ ] 1b. Pass `timeout=cfg.models.upstream_timeout_secs`
        to every `litellm.acompletion` call in
        `gateway/src/gateway/router.py`. Streaming and
        non-streaming, plus the agent loop's call.
- [ ] 1c. Refactor the chat dispatch to construct
        `dispatch_task = asyncio.create_task(...)`
        and await it through
        `asyncio.wait_for(asyncio.shield(dispatch_task),
        timeout=cfg.models.upstream_timeout_secs)`.
        The `shield` is the load-bearing decision: it
        keeps the orphan task alive after the
        `wait_for` cancellation, even though we don't
        use it yet. A future commit attaches a reaper
        with one line at this site.
- [ ] 1d. On `TimeoutError`: log
        `chat.completion status=upstream_silent` with
        `request_id`, `alias`, `timeout_secs`, then
        return 503 with body
        `{"error": {"type": "upstream_silent",
        "timeout_secs": N, "alias": ...,
        "message": "Upstream <alias> went silent
        after <N>s — likely queued.",
        "request_id": ...}}`.
- [ ] 1e. The same wrapper applies in the tool-loop
        path. The existing `run_with_detach` already
        wraps the loop coroutine; nest the upstream
        timeout *inside* it (the detach threshold,
        if configured, is independent and stays
        responsible for the approval-pending case).
- [ ] 1f. Test: stub LiteLLM to block forever,
        configure 0.1s upstream timeout, send chat
        request, assert HTTP 503 with the right
        error body within ~0.5s.
- [ ] 1g. Test: stub LiteLLM to return after 0.5s
        (longer than configured timeout but fast
        enough to dodge the bot's read-timeout).
        Assert the bot still got the timeout
        response — the orphan continues but is
        silently GC'd. The shielded dispatch_task
        contract is what we're pinning here; the
        reaper that turns this into useful audit
        data is deferred.

## Commit 2: Bot speaks the new error vocabulary

Goal: bot's `chat()` distinguishes the four user-
facing failure shapes, branches on the gateway's
`error.type` (not on string matching), and includes
the short request_id in every user-visible message
so users can paste it in bug reports.

- [ ] 2a. In
        `telegram-bot/src/fitt_telegram_bot/gateway_client.py`,
        split the `except httpx.RequestError` branch
        in `chat()` into:
        - `except (httpx.ConnectError,
          httpx.ConnectTimeout)`:
          `failure_kind="connect_failure"`,
          user message
          "⚠️ FITT gateway unreachable: <ExcClass>".
        - `except (httpx.ReadTimeout,
          httpx.WriteTimeout, httpx.PoolTimeout)`:
          `failure_kind="bot_read_timeout"`,
          user message
          "⏱️ FITT didn't respond in time on the bot
          side. Configuration drift — see
          telegram-bot.log for details."
        - `except httpx.RequestError`:
          `failure_kind="transport"`, generic
          "⚠️ Network error reaching FITT: …"
- [ ] 2b. In `_format_error`, branch on the
        gateway's `error.type` field (not on string
        matching of the message). New case for
        `upstream_silent`:
        "⏱️ Upstream `<alias>` went silent for
        <N>s — likely queued. Try again, or pick
        a different alias."
        Existing types
        (`upstream_rate_limited`,
        `upstream_client_error`,
        `upstream_server_error`,
        `no_backend_available`) keep their
        translations but get refactored into the
        same branch dispatch.
- [ ] 2c. Append the short `request_id` (first 8
        chars from the gateway's response header)
        to every yielded ⚠️ message. Format
        consistently across types:
        "(req: a1b2c3d4)".
- [ ] 2d. Tests: pin each `error.type` → user
        string mapping in
        `tests/test_gateway_client.py`. Pin the
        request_id appears verbatim in the
        message.
- [ ] 2e. Test: with the gateway returning
        `upstream_silent`, the bot's structured
        log row carries `failure_kind="http_error"`,
        `error_type="upstream_silent"`,
        `error_detail` with the timeout number.

## Commit 3: Sane defaults and minimal docs

Goal: ship the spec without enforcement, with
documented intent. Future operators (and future
me) read the configs and see what the invariant is
even though nothing checks it at boot.

- [ ] 3a. Set bot's `_STREAM_TIMEOUT_S = 360.0`
        (was 120.0). Comment at the constant
        explains the gateway-side invariant: bot
        read-timeout must be > gateway
        `models.upstream_timeout_secs`. Default
        gateway = 300, default bot = 360, 60s
        margin.
- [ ] 3b. Update `configs/config.example.yaml`
        with the `upstream_timeout_secs: 300`
        entry under `models:` and a 4-5 line
        comment explaining the invariant and the
        default margin.
- [ ] 3c. Add a "What happens on upstream
        timeout" subsection to
        `docs/quickstart.md` troubleshooting:
        - User sees `⏱️ Upstream <alias> went
          silent after Ns` → expected behavior,
          retry or switch alias.
        - User sees `⚠️ FITT gateway unreachable`
          → gateway is actually down (`docker
          compose ps`).
        - User sees `⏱️ FITT didn't respond in
          time on the bot side` → invariant
          violation, check both timeouts.
- [ ] 3d. Update `gateway/README.md` config
        reference with the new
        `models.upstream_timeout_secs` field and
        the bot-side invariant.

## Verification

- [ ] 4a. After commits 1-3 land, configure
        gateway upstream timeout to 60s on the
        NAS. Send chat to `fitt-smart` (still
        routed to NVIDIA). Confirm:
        - Bot shows the upstream-silent message
          with timeout number and request_id
          short tag.
        - `gateway.log` has
          `status=upstream_silent` row with the
          same request_id.
        - Bot's `telegram-bot.log` has
          `gateway.chat.failed` row with
          `failure_kind="http_error"`,
          `error_type="upstream_silent"`,
          same request_id.
- [ ] 4b. Restore upstream_timeout_secs to 300
        (or whatever you settle on for everyday
        use).

## Deferred — see design.md "Future migration paths"

The following pieces have spec coverage in
design.md but no tasks here. Each is a clean
addition on top of the work above.

- **Upstream reaper** that logs late outcomes
  (`chat.upstream_late_outcome`). ~3 hours.
  Useful only if you keep using FITT and want
  audit data on whether late successes are
  common. Add when you want to know the answer.
- **Bot startup invariant validation** via
  `X-FITT-Upstream-Timeout` response header.
  ~1 hour. Skip unless you start seeing
  invariant-violation messages in real use.
- **Late delivery to the user** (Option 1 in
  design.md). ~1 day on top of the reaper.
  Promote when reaper data shows it'd actually
  help.
- **Cancellation of in-flight LiteLLM requests**
  (Option 3 in design.md). ~half day plus
  verification. Skip unless token waste
  becomes a real cost.
- **Per-alias timeout overrides**. ~30 min.
  Add when one alias's "right" timeout
  diverges enough from the global default that
  it matters.

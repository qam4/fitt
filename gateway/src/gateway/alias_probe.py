"""Boot-time alias tool-call reliability probe (Principle 11).

Why
---

FITT's whole value prop downstream of Phase 4 depends on the bound
model emitting real OpenAI-shape ``tool_calls`` structures when a
tool would help. Two 2026 incidents surfaced what happens when a
binding silently fails this contract:

* **2026-05-07, qwen2.5-coder:14b on local Ollama.** Every cron
  firing and Telegram tool-use turn narrated JSON-looking tool
  calls in the reply content instead of emitting real
  ``tool_calls``. The gateway did the right thing (didn't parse
  narration as a call); the user experience was the model
  answering "yes I did the thing" without actually doing it.
* **2026-05-10, qwen/qwen3-next-80b-a3b-instruct via NIM.** Hit
  the sentinel narration pattern (``TOOL_NAME: ... BEGIN_ARG: ...
  END_ARG:``) mid-session, poisoning history so later turns
  mirrored it. MoE sparse activation pattern; shows up
  intermittently, invisible until you look at raw history.

Both incidents were detectable on the first tool-use turn. We
didn't detect them because nothing was probing. Principle 11
says: fail loud on detectable misconfigurations at boot.

What this does
--------------

At gateway startup, for each alias:

1. Build a minimal OpenAI chat request with one synthetic tool
   (``_fitt_probe``) in ``tools`` and ``tool_choice="auto"``, plus
   a user message telling the model to call it.
2. Dispatch via :class:`AliasRouter` exactly the way a real chat
   would — same backend path, same api keys, same LiteLLM config.
3. Inspect the response. Shape-level signal, not content regex:

   * ``tool_calls`` present + well-formed → ``ok``.
   * Non-empty reply + no ``tool_calls`` + clean finish → ``narrated``
     (model narrated a call instead of emitting one). This is the
     exact signal :func:`capabilities.is_tool_use_expected_but_none`
     uses at runtime, applied proactively.
   * ``finish_reason="length"`` or other non-stop cutoff →
     ``truncated`` (not a tool-call failure per se, but operator
     should know).
   * Empty reply + no ``tool_calls`` + clean finish →
     ``empty_reply`` (the model said nothing and called nothing;
     the dispatch succeeded, so this is a model-behavior anomaly,
     not a transport problem).
   * Dispatch failure → one of the shared
     :mod:`gateway.dispatch_outcome` statuses
     (``upstream_silent`` / ``upstream_rate_limited`` /
     ``upstream_client_error`` / ``upstream_server_error``), or —
     on a *timeout*, after a reachability ping — ``upstream_silent``
     (host reachable, model slow / cold-loading) vs ``unreachable``
     (host down). Phase 7.6 replaced the old catch-all
     ``transport_error`` with this taxonomy so the operator can
     tell a VRAM-contended laptop from a dead host.

4. Return a :class:`ProbeResult` per alias so the caller can log
   one ERROR line per non-``ok`` alias and move on.

What this does NOT do
---------------------

* **No refusal to start.** One misbehaving alias shouldn't block
  the whole gateway — other aliases might be fine, and refusing
  to start would make this check worse than the silent failure
  it replaces. Callers log, then continue.
* **No real tool execution.** The synthetic ``_fitt_probe`` tool
  is a pure schema; the probe never drives the tool loop past
  the first response. We observe the shape of the model's reply
  and stop.
* **No full eval harness.** That's a separate item (see
  ``docs/hallucinations-and-poisoning.md`` proposed item 6). The
  boot probe is a single canary per alias; the eval harness is
  the curated suite the operator runs on demand after a swap.
  This module's logic will be reused there; keeping it decoupled
  from the HTTP layer is the entire point.

Cost
----

One request per alias at startup. For typical FITT setups that's
2-4 aliases x a few hundred tokens x one round trip. Free on
local Ollama; negligible on NIM / OpenRouter. Worth it — you only
pay on reboot, and the alternative is finding out during a live
Telegram turn what a five-second probe would have surfaced
cleanly.

Skipped aliases
---------------

The probe skips an alias when:

* Its backend needs an api key and the key is missing. The
  api_keys check already logged an ERROR for this case
  (see :func:`gateway.config.check_missing_api_keys`); re-probing
  would just log a duplicate dispatch failure.
* The gateway operator disabled probes via
  ``server.boot_probe_enabled = false``. For tests that want to
  construct an app without network calls, and for operators who
  don't want the startup latency.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal

from .agent_loop import (
    assistant_message_from_response,
    extract_tool_calls,
    response_to_dict,
)
from .dispatch_outcome import classify_dispatch_exception
from .reachability import check_reachable_standalone

if TYPE_CHECKING:
    from .config import Config
    from .router import AliasRouter

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- canary

_PROBE_TOOL_NAME = "_fitt_probe"
"""Leading underscore + namespace prefix so this name can't
collide with a real FITT tool. The model sees this name in the
``tools`` array for one request and never again."""

_PROBE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _PROBE_TOOL_NAME,
        "description": (
            "Internal FITT boot-time reliability probe. Call this "
            "tool once with no arguments to confirm tool-calling "
            "works on this alias; then stop."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}

_PROBE_USER_MESSAGE = (
    "FITT boot probe. Call the `_fitt_probe` tool once with no "
    "arguments, then stop. Do not narrate, do not explain, do "
    "not reply with text. Your only job is to emit a real "
    "tool_calls structure invoking `_fitt_probe()`."
)

_MIN_ASSISTANT_REPLY_FOR_NARRATION = 30
"""Matches the runtime threshold used in
:func:`capabilities.is_tool_use_expected_but_none`. Below this
length, a short reply is ambiguous (could be a polite ack,
"ok", etc.) and we don't call it narration."""


# --------------------------------------------------------------- result


ProbeStatus = Literal[
    "ok",
    "narrated",
    "truncated",
    "upstream_silent",
    "unreachable",
    "upstream_rate_limited",
    "upstream_client_error",
    "upstream_server_error",
    "empty_reply",
    "skipped_no_api_key",
    "disabled",
]
"""Probe outcome status.

Success-shape statuses (``ok`` / ``narrated`` / ``truncated``)
describe how the model replied. The dispatch-failure statuses
(``upstream_silent`` / ``unreachable`` / ``upstream_rate_limited``
/ ``upstream_client_error`` / ``upstream_server_error``) come from
the shared :mod:`gateway.dispatch_outcome` taxonomy — Phase 7.6
replaced the old catch-all ``transport_error`` with these so the
operator can tell "slow / cold-loading" (``upstream_silent``)
from "host is down" (``unreachable``). ``empty_reply`` is the
model-said-nothing-and-called-nothing anomaly (formerly folded
into ``transport_error``)."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One alias's probe outcome.

    ``status`` drives log-line shape; ``detail`` is a short
    human-readable one-liner operators can paste into a bug
    report. ``model_used`` identifies the concrete model that
    served the probe (after fallback resolution); absent when
    the probe didn't get that far (skipped, disabled).

    ``latency_ms`` (Phase 7.6) is the wall-clock the dispatch
    took — for a VRAM-contended local setup this *is* the health
    signal (``ok`` at 1.2s vs 8.9s is "healthy" vs "watch this").
    On a timeout it sits at the budget ceiling. ``reachable``
    (Phase 7.6) records the reachability-ping verdict when the
    probe ran one (i.e. after a timeout); ``None`` when no ping
    was needed."""

    alias: str
    status: ProbeStatus
    detail: str
    latency_ms: int = 0
    model_used: str | None = None
    finish_reason: str | None = None
    reply_preview: str = ""
    """First ~200 chars of the assistant reply when the model
    narrated instead of calling. Included so the log line is
    self-contained — operators grepping for
    ``alias_probe.narrated`` see the actual failure shape
    without cross-referencing anything else."""
    reachable: bool | None = None


# --------------------------------------------------------------- probe


async def probe_alias(
    alias: str,
    router: AliasRouter,
    *,
    timeout_s: float = 10.0,
    config: Config | None = None,
) -> ProbeResult:
    """Run one canary tool-call request against ``alias``.

    Returns a :class:`ProbeResult`. Does not raise on failure —
    dispatch exceptions and timeouts become classified failure
    results so the caller can log uniformly across all aliases.

    Phase 7.6: failures are classified via the shared
    :mod:`gateway.dispatch_outcome` taxonomy instead of a flat
    ``transport_error``. On a *timeout* specifically, the probe
    runs a reachability ping against the resolved model's
    endpoint (reusing :func:`gateway.reachability.check_reachable_standalone`)
    to tell ``upstream_silent`` (host reachable, model just slow
    / cold-loading) from ``unreachable`` (host down). ``config``
    is needed to resolve the alias's model for the reachability
    ping; when ``None`` (older callers / tests that don't supply
    it), a timeout falls back to ``upstream_silent`` without the
    disambiguating ping.
    """
    request_body: dict[str, Any] = {
        "messages": [{"role": "user", "content": _PROBE_USER_MESSAGE}],
        "tools": [_PROBE_TOOL_SCHEMA],
        "tool_choice": "auto",
        "stream": False,
        # Keep the probe fast and cheap. Some backends error if
        # max_tokens is 0 so give the model headroom to emit a
        # small tool_calls structure. 256 fits the largest
        # realistic shape with room to spare.
        "max_tokens": 256,
    }

    started = perf_counter()

    def _elapsed_ms() -> int:
        return int((perf_counter() - started) * 1000)

    try:
        result = await asyncio.wait_for(router.dispatch(alias, request_body), timeout=timeout_s)
    except TimeoutError:
        # The dispatch didn't answer in time. That's ambiguous on
        # its own: the host could be cold-loading a model (slow
        # but reachable) or genuinely down. Disambiguate with a
        # cheap reachability ping when we can resolve the model.
        latency_ms = _elapsed_ms()
        return await _classify_timeout(alias, timeout_s, latency_ms, config)
    except Exception as exc:
        # Any non-timeout dispatch failure (transport, auth, 5xx).
        # Classify via the shared taxonomy so the status matches
        # what the chat path would report for the same exception.
        outcome = classify_dispatch_exception(exc)
        return ProbeResult(
            alias=alias,
            status=outcome.status,
            detail=f"{outcome.error_class}: {outcome.error_detail}",
            latency_ms=_elapsed_ms(),
        )

    latency_ms = _elapsed_ms()
    model_used_id = result.model_used.id
    response = result.response  # non-streaming — see request_body
    tool_calls = extract_tool_calls(response)
    if tool_calls:
        return ProbeResult(
            alias=alias,
            status="ok",
            detail=f"emitted {len(tool_calls)} tool call(s) as expected",
            latency_ms=latency_ms,
            model_used=model_used_id,
        )

    # No tool_calls. Figure out why.
    assistant_msg = assistant_message_from_response(response)
    reply = ""
    if isinstance(assistant_msg, dict):
        content = assistant_msg.get("content")
        if isinstance(content, str):
            reply = content

    # finish_reason lives on choices[0], not the message.
    finish_reason: str | None = None
    dumped = response_to_dict(response)
    if dumped:
        choices = dumped.get("choices")
        if isinstance(choices, list) and choices:
            choice0 = choices[0]
            if isinstance(choice0, dict):
                fr = choice0.get("finish_reason")
                if isinstance(fr, str):
                    finish_reason = fr

    if finish_reason == "length":
        return ProbeResult(
            alias=alias,
            status="truncated",
            detail="model hit max_tokens before emitting tool_calls",
            latency_ms=latency_ms,
            model_used=model_used_id,
            finish_reason=finish_reason,
            reply_preview=_preview(reply),
        )

    if len(reply) >= _MIN_ASSISTANT_REPLY_FOR_NARRATION:
        return ProbeResult(
            alias=alias,
            status="narrated",
            detail=(
                "model replied with text instead of emitting a "
                f"tool_calls structure (reply {len(reply)} chars)"
            ),
            latency_ms=latency_ms,
            model_used=model_used_id,
            finish_reason=finish_reason,
            reply_preview=_preview(reply),
        )

    # Empty or near-empty reply + no tool_calls. Unusual shape
    # (the model said nothing AND called no tool). Phase 7.6:
    # this is its own ``empty_reply`` status rather than being
    # folded into a transport error — the dispatch *succeeded*,
    # the model just produced nothing useful, which is a
    # model-behavior anomaly, not a transport problem.
    return ProbeResult(
        alias=alias,
        status="empty_reply",
        detail="model returned empty reply with no tool_calls",
        latency_ms=latency_ms,
        model_used=model_used_id,
        finish_reason=finish_reason,
    )


async def _classify_timeout(
    alias: str,
    timeout_s: float,
    latency_ms: int,
    config: Config | None,
) -> ProbeResult:
    """Turn a probe timeout into ``upstream_silent`` (reachable)
    or ``unreachable`` (host down) via a reachability ping.

    When ``config`` is unavailable we can't resolve the model to
    ping, so we report ``upstream_silent`` — the conservative
    choice (a timeout most often means slow, not down, and we
    don't want to cry "unreachable" without evidence)."""
    if config is None:
        return ProbeResult(
            alias=alias,
            status="upstream_silent",
            detail=f"probe timed out after {int(timeout_s)}s (reachability not checked)",
            latency_ms=latency_ms,
        )

    try:
        primary = config.resolve_alias(alias)[0]
    except Exception:
        return ProbeResult(
            alias=alias,
            status="upstream_silent",
            detail=f"probe timed out after {int(timeout_s)}s (alias unresolved)",
            latency_ms=latency_ms,
        )

    reach = await check_reachable_standalone(primary)
    if reach.reachable:
        return ProbeResult(
            alias=alias,
            status="upstream_silent",
            detail=(
                f"probe timed out after {int(timeout_s)}s but endpoint is "
                f"reachable ({reach.latency_ms}ms ping) — model is likely "
                "cold-loading or queuing for VRAM"
            ),
            latency_ms=latency_ms,
            model_used=primary.id,
            reachable=True,
        )
    return ProbeResult(
        alias=alias,
        status="unreachable",
        detail=(
            f"probe timed out after {int(timeout_s)}s and endpoint is "
            f"unreachable: {reach.detail or 'no response'}"
        ),
        latency_ms=latency_ms,
        model_used=primary.id,
        reachable=False,
    )


# --------------------------------------------------------------- batch


async def probe_all_aliases(
    config: Config,
    router: AliasRouter,
    *,
    timeout_s: float = 10.0,
) -> list[ProbeResult]:
    """Probe every alias in ``config.aliases``. Runs probes
    concurrently — there's no contention across aliases
    (different backends, or same backend with different
    ratelimits) and a five-alias gateway shouldn't wait 25s at
    boot because we insisted on serial."""
    aliases = config.alias_names()
    secrets = config.secrets
    tasks: list[asyncio.Task[ProbeResult]] = []

    for alias in aliases:
        # Skip aliases whose primary model needs an api key we
        # don't have. The api_keys check already logged this.
        chain = config.resolve_alias(alias)
        primary = chain[0]
        if primary.backend in ("openai", "openrouter", "anthropic"):
            # Local Ollama is the only backend that doesn't need a
            # key; everything else expects one.
            if secrets is None:
                # No secrets loaded at all; can't probe anything
                # that wants auth. Return one skipped result for
                # consistency.
                tasks.append(asyncio.create_task(_immediate_skip(alias, "secrets not loaded")))
                continue
            key = secrets.api_key_for(primary.backend, model_id=primary.id)
            if key is None and primary.backend == "openai":
                # Already logged by check_missing_api_keys.
                tasks.append(
                    asyncio.create_task(
                        _immediate_skip(
                            alias,
                            f"no api_keys.{primary.id} entry",
                        )
                    )
                )
                continue
        tasks.append(
            asyncio.create_task(probe_alias(alias, router, timeout_s=timeout_s, config=config))
        )

    return list(await asyncio.gather(*tasks))


async def _immediate_skip(alias: str, detail: str) -> ProbeResult:
    """Helper so ``probe_all_aliases`` can return a skipped-no-key
    result via the same ``gather`` machinery as live probes. The
    async layer is purely about uniform scheduling — there's no
    IO in here."""
    return ProbeResult(
        alias=alias,
        status="skipped_no_api_key",
        detail=detail,
    )


# --------------------------------------------------------------- helpers


def _preview(text: str, *, cap: int = 200) -> str:
    """Short preview of an assistant reply for log lines.

    Keeps the first ``cap`` chars, collapses whitespace so a
    sentinel-narrated reply that spans five lines shows up as
    one readable line. Truncated with ``[...]`` when over the
    cap."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= cap:
        return collapsed
    return collapsed[: cap - 5] + "[...]"

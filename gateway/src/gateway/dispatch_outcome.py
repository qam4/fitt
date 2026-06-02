"""Shared dispatch-outcome taxonomy (Phase 7.6).

One vocabulary for "why did a model dispatch fail", shared by
the chat path (live requests), the alias probe (boot / on-demand
canary), and the eval harness. Before this module existed, each
of those classified failures differently — the chat path had the
mature taxonomy (``upstream_silent`` / ``upstream_rate_limited``
/ ...), while the probe and eval flattened everything into a
single ``transport_error`` that read as "can't reach the host"
even when the host was fine and the model was merely
cold-loading.

The canonical classifier here is lifted verbatim from the chat
path's former ``chat.py::_classify_upstream_error``; chat.py now
imports it (via a thin dict-returning adapter so its existing
``log_request`` call sites are untouched). The probe and eval
adopt it for their failure side.

Status meanings
---------------

* ``upstream_silent`` — LiteLLM (or the underlying httpx) hit
  our configured timeout. The upstream went quiet for longer
  than we were willing to wait. For a local Ollama backend this
  usually means the model is cold-loading or queuing for VRAM,
  not that the host is down — which is exactly why the probe
  follows a timeout with a reachability ping (Phase 7.6
  Decision 2) before deciding between ``upstream_silent`` and
  ``unreachable``.
* ``upstream_rate_limited`` — 429/529 from upstream.
  ``upstream_status`` carries the actual code; ``retry_after``
  records the parsed Retry-After header (or a synthesized
  default).
* ``upstream_client_error`` — other 4xx (auth, bad request).
* ``upstream_server_error`` — 5xx, transport failures
  (connection reset, read timeout), DNS failures, anything that
  doesn't expose a ``status_code`` attribute. Catch-all.
* ``unreachable`` — confirmed can't-connect. Only the probe
  emits this: it's ``upstream_server_error`` refined by a
  successful-or-failed reachability ping. The chat path never
  runs that ping, so it never emits ``unreachable`` (it keeps
  classifying connection failures as ``upstream_server_error``).
  That's intentional asymmetry, not drift — a consumer with
  more information (the probe ran the ping) can be more
  specific.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DispatchStatus = Literal[
    "upstream_silent",
    "upstream_rate_limited",
    "upstream_client_error",
    "upstream_server_error",
    "unreachable",
]


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """Structured classification of a dispatch exception.

    ``status`` is the taxonomy bucket; ``error_class`` is the
    Python exception class name (so an operator can tell NVIDIA
    queue depth from a Tailscale flap); ``error_detail`` is the
    truncated exception message (preserving e.g. NVIDIA's
    "368 in queue" body without response-body capture);
    ``upstream_status`` is the HTTP status when one was exposed;
    ``retry_after`` is the parsed/synthesized Retry-After for
    the rate-limited case.
    """

    status: DispatchStatus
    error_class: str
    error_detail: str
    upstream_status: int | None = None
    retry_after: str | None = None

    def to_log_fields(self) -> dict[str, Any]:
        """Render as the dict shape ``log_request`` consumes.

        Keyed exactly as the former ``_classify_upstream_error``
        returned: ``error_type`` (not ``status``), ``error_class``,
        ``error_detail``, and the optional ``upstream_status`` /
        ``retry_after`` only when set. This is what keeps the
        chat path's structured logs byte-identical after the
        extraction."""
        fields: dict[str, Any] = {
            "error_type": self.status,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
        }
        if self.upstream_status is not None:
            fields["upstream_status"] = self.upstream_status
        if self.retry_after is not None:
            fields["retry_after"] = self.retry_after
        return fields


def classify_dispatch_exception(exc: Exception) -> DispatchOutcome:
    """Classify an upstream-dispatch exception into the shared
    taxonomy.

    Lifted verbatim from the chat path's former
    ``_classify_upstream_error`` so the routing logic — and
    therefore the structured-log and user-facing-HTTP shapes
    that mirror it — can't drift. Never raises; always returns
    exactly one :class:`DispatchOutcome`.

    Note: this function never returns ``unreachable``. That
    status is the probe's refinement of ``upstream_server_error``
    after a reachability ping; see the module docstring.
    """
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    resp = getattr(exc, "response", None)
    retry_after: str | None = None
    if resp is not None:
        headers = getattr(resp, "headers", {}) or {}
        retry_after = headers.get("retry-after") or headers.get("Retry-After")

    error_class = type(exc).__name__
    error_detail = message[:500] if isinstance(message, str) else str(message)[:500]

    # Phase 4.9: LiteLLM raises ``litellm.Timeout`` (with
    # ``status_code=408`` per their convention) when the
    # ``timeout=`` kwarg fires. We treat that as upstream_silent
    # rather than letting it fall through to upstream_client_error
    # — 408 from the upstream itself would be unusual and is
    # operationally the same shape (we couldn't get an answer in
    # time).
    is_litellm_timeout = type(exc).__name__ == "Timeout" or (
        status == 408 and "timeout" in message.lower()
    )
    if is_litellm_timeout:
        return DispatchOutcome(
            status="upstream_silent",
            error_class=error_class,
            error_detail=error_detail,
        )
    if status in (429, 529):
        return DispatchOutcome(
            status="upstream_rate_limited",
            error_class=error_class,
            error_detail=error_detail,
            upstream_status=status,
            retry_after=retry_after or ("30" if status == 529 else "5"),
        )
    if isinstance(status, int) and 400 <= status < 500:
        return DispatchOutcome(
            status="upstream_client_error",
            error_class=error_class,
            error_detail=error_detail,
            upstream_status=status,
        )
    return DispatchOutcome(
        status="upstream_server_error",
        error_class=error_class,
        error_detail=error_detail,
        upstream_status=status if isinstance(status, int) else None,
    )

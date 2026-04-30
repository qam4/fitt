"""Alias router — resolves aliases to models and dispatches via LiteLLM.

This module is the heart of Phase 1. It:

1. Looks up the primary+fallback chain for an alias.
2. Builds LiteLLM-flavoured kwargs for each candidate model.
3. Tries the primary; on transport failure, tries the fallback exactly
   once.
4. Surfaces the backend that actually served the request so the HTTP
   layer can tag the response with ``X-FITT-Backend``.

Design decisions explicit in this code:

* **One-level fallback only** (Decision 6 in design.md). No cascade.
* **No auto-retry on rate-limit or overload.** Rate-limit failures are
  surfaced to the client as 503 + Retry-After (done in ``chat.py``).
* **Streaming preserves upstream chunk order byte-for-byte** modulo
  OpenAI envelope rewriting (LiteLLM handles the envelope).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import litellm

from .config import Backend, Config, ModelConfig
from .errors import NoBackendAvailable, UnknownAlias


@dataclass
class DispatchResult:
    """Outcome of a dispatch call.

    Either ``response`` or ``stream`` is set, never both. ``model_used``
    identifies which candidate model actually served the request — this
    is what populates the ``X-FITT-Backend`` header.
    """

    response: Any | None  # litellm ModelResponse, for non-streaming
    stream: AsyncIterator[Any] | None  # async iterator, for streaming
    model_used: ModelConfig
    fallback_used: bool


def _litellm_kwargs(model: ModelConfig, secrets_key: str | None) -> dict[str, Any]:
    """Build LiteLLM kwargs for one candidate model."""
    kwargs: dict[str, Any] = {}
    match model.backend:
        case "ollama":
            # LiteLLM's ollama_chat provider matches ollama's /api/chat
            # endpoint and supports tool calls + streaming cleanly.
            kwargs["model"] = f"ollama_chat/{model.model}"
            assert model.endpoint
            kwargs["api_base"] = model.endpoint
        case "openrouter":
            kwargs["model"] = f"openrouter/{model.model}"
            if secrets_key:
                kwargs["api_key"] = secrets_key
        case "anthropic":
            kwargs["model"] = f"anthropic/{model.model}"
            if secrets_key:
                kwargs["api_key"] = secrets_key
        case "openai":
            # Generic OpenAI-compatible endpoint: Nvidia Build,
            # Groq, Together, LM Studio, vLLM, and anything else
            # that speaks the OpenAI schema. LiteLLM uses the
            # "openai/<model>" prefix with an explicit api_base.
            kwargs["model"] = f"openai/{model.model}"
            assert model.endpoint
            kwargs["api_base"] = model.endpoint
            if secrets_key:
                kwargs["api_key"] = secrets_key
    return kwargs


# Exceptions that signal "transport failure — try the fallback."
# Upstream 4xx/5xx are NOT here: those are semantic failures, not
# transport failures, so we surface them directly without retrying.
_TRANSPORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    ConnectionError,
)


def _is_transport_failure(exc: BaseException) -> bool:
    """Best-effort classifier for 'try the fallback' conditions."""
    if isinstance(exc, _TRANSPORT_EXCEPTIONS):
        return True
    # LiteLLM wraps upstream errors; check the cause and the class name.
    if exc.__cause__ is not None and isinstance(exc.__cause__, _TRANSPORT_EXCEPTIONS):
        return True
    # LiteLLM-specific: APIConnectionError subclasses vary by version.
    cls_name = type(exc).__name__
    return cls_name in {
        "APIConnectionError",
        "Timeout",
        "ServiceUnavailableError",
    }


class AliasRouter:
    """Resolve aliases and dispatch requests to LiteLLM."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def resolve(self, alias: str) -> list[ModelConfig]:
        """Return the primary+fallback chain, raising UnknownAlias if missing."""
        try:
            return self._config.resolve_alias(alias)
        except KeyError as e:
            raise UnknownAlias(alias, self._config.alias_names()) from e

    async def dispatch(
        self,
        alias: str,
        openai_request: dict[str, Any],
    ) -> DispatchResult:
        """Dispatch an OpenAI-shaped request to the alias's backend.

        ``openai_request`` is the raw JSON body from the client (with
        ``messages``, ``stream``, ``temperature``, etc.). We strip the
        ``model`` field because LiteLLM needs the backend-specific
        model name from our config.
        """
        chain = self.resolve(alias)
        secrets = self._config.secrets
        assert secrets is not None, "secrets must be loaded before dispatch"

        # Copy, strip client-supplied 'model'.
        body = {k: v for k, v in openai_request.items() if k != "model"}

        last_transport_exc: BaseException | None = None
        attempted_ids: list[str] = []

        for idx, candidate in enumerate(chain):
            attempted_ids.append(candidate.id)
            key = secrets.api_key_for(candidate.backend, model_id=candidate.id)
            kwargs = _litellm_kwargs(candidate, key)
            call_kwargs = {**body, **kwargs}

            try:
                result = await litellm.acompletion(**call_kwargs)
            except _TRANSPORT_EXCEPTIONS as e:
                last_transport_exc = e
                continue
            except Exception as e:
                # Semantic failure (4xx/5xx from upstream). If it's a
                # transport failure in disguise, try the fallback;
                # otherwise re-raise so the chat handler can translate
                # it to the right status code.
                if _is_transport_failure(e):
                    last_transport_exc = e
                    continue
                raise

            fallback_used = idx > 0
            if body.get("stream"):
                return DispatchResult(
                    response=None,
                    stream=result,
                    model_used=candidate,
                    fallback_used=fallback_used,
                )
            return DispatchResult(
                response=result,
                stream=None,
                model_used=candidate,
                fallback_used=fallback_used,
            )

        # Every candidate had a transport failure.
        raise NoBackendAvailable(alias, attempted_ids) from last_transport_exc


def backend_tag(model: ModelConfig) -> str:
    """Short string for the X-FITT-Backend header.

    Examples:
      * ``openrouter:anthropic/claude-sonnet-4.5``
      * ``ollama:http://laptop.tailnet:11434``
      * ``anthropic:claude-sonnet-4-5``
    """
    b: Backend = model.backend
    if b == "ollama":
        return f"ollama:{model.endpoint}"
    return f"{b}:{model.model}"

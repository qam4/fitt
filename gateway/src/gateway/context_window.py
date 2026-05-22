"""Per-binding context window discovery (Phase 7, Slice 7.1).

Why
---

Compaction (Phase 8) needs a real ceiling. ``/model`` and the
dashboard (Phase 7 Slices 7.3 / 7.5) need the same number to
say "the prompt is at 23% of the context window" rather than
"the prompt is 5400 tokens, you figure out if that's a lot."

Today FITT has no awareness of per-binding context windows.
Operators set them out-of-band — Ollama via
``OLLAMA_CONTEXT_LENGTH`` env var or per-model modelfile
override; cloud providers via the model card. The 2026-05-22
granite incident surfaced this gap directly: even after
diagnosing the system-prompt-size issue, we had no way to tell
from inside FITT whether granite was at 1% or 99% of its
ceiling.

This module owns discovery. One async function per backend
shape, a cache that holds the results for the lifetime of the
gateway process, a refresh path for the operator-changed-the-
backend-config case.

What this module does NOT do
----------------------------

* **Block startup.** Discovery is best-effort. A failure logs
  loud and stores ``tokens=None``; chat dispatch reads the
  cache and proceeds whether the number is known or not.
* **Run periodically.** No background poll. Discovery runs at
  boot and on operator command (``fitt context refresh``).
  Models don't change their context window during the
  process's lifetime; if the operator changes the backend
  config they re-run discovery.
* **Pre-dispatch token counting.** That's a Phase 8 concern.
  Slice 7.1 surfaces the ceiling; Slice 7.2 records the
  observed prompt size; compaction in Phase 8 compares the
  two.

Per-backend probes
------------------

* **Ollama.** ``POST /api/show`` returns a body with the
  modelfile parameters and architecture metadata. Prefer the
  modelfile's ``num_ctx`` parameter when set; fall back to the
  architecture's ``model_info["<arch>.context_length"]``
  natural ceiling; fall back to 2048 (Ollama's documented
  default) with a WARNING because that's the
  "operator-forgot-to-set-OLLAMA_CONTEXT_LENGTH" case the
  granite-style incidents are made of.
* **OpenAI-compatible** (``openai`` / ``openrouter`` per the
  config Backend enum, plus generic NIM / Groq / Together via
  ``openai`` backend). ``GET /v1/models`` returns a list with
  ``context_length`` (or ``max_input_tokens`` on OpenRouter)
  per model. Match by model id.
* **Anthropic.** No discovery endpoint that returns context
  length. Static lookup table keyed on family prefix.

Privacy posture: probes pass through bearer tokens or api keys
the same way the live dispatch path does (LiteLLM-compatible
shapes). No secret values are logged; only the shape and source
of the discovered result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

from .config import Backend, Config, ModelConfig, Secrets

_log = logging.getLogger(__name__)


# Provenance values for ``ContextWindowResult.source``. Stable
# strings so operator surfaces (``/model``, dashboard,
# ``fitt context list``) can branch on them.
ContextWindowSource = Literal[
    "modelfile",  # Ollama: num_ctx parameter set in modelfile
    "model_info",  # Ollama: architecture's natural context_length
    "api_models",  # OpenAI-shape: /v1/models field
    "lookup_table",  # Anthropic: static family map
    "default",  # Backend default (e.g. Ollama 2048)
    "unknown",  # Discovery failed
]


@dataclass(frozen=True, slots=True)
class ContextWindowResult:
    """One binding's context-window discovery outcome.

    ``tokens`` is None when discovery failed. ``source`` carries
    the provenance so the operator can tell "the modelfile said
    so" from "we fell back to a 2048 default because nothing
    parseable was returned".
    """

    tokens: int | None
    source: ContextWindowSource
    detail: str
    discovered_at: float


# Anthropic family lookup. Adding a new family is a one-line
# edit — Anthropic doesn't expose context length via API, so
# this is the operational answer. Update when a new Anthropic
# generation ships.
_ANTHROPIC_CONTEXT_BY_FAMILY: dict[str, int] = {
    "claude-sonnet-4": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    # Earlier generations kept for completeness; updates can
    # remove these once the corresponding aliases retire.
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
}

_OLLAMA_DEFAULT_CONTEXT = 2048
"""Ollama's documented default when no num_ctx is set in the
modelfile and no architecture context_length is reported.
Tagged ``source="default"`` so operators can grep their boot
logs for fallbacks they didn't intend."""


class ContextWindowProbe(Protocol):
    """Discovery contract per backend.

    Probes are stateless apart from what they need to make the
    HTTP call (api keys come in via ``secrets``). They must not
    raise — every failure mode produces a
    :class:`ContextWindowResult` with ``tokens=None`` and a
    descriptive ``detail``. The agent loop must never see a
    raised exception from this layer.
    """

    backend: Backend

    async def discover(
        self,
        model: ModelConfig,
        secrets: Secrets | None,
        *,
        timeout_s: float,
    ) -> ContextWindowResult: ...


# --------------------------------------------------------------- ollama


class OllamaContextProbe:
    """Discover via ``POST /api/show``.

    Three signal layers (preferred first):

    1. ``parameters`` text on the response — operator-set
       ``num_ctx <N>``. This is the configured ceiling Ollama
       will actually use at inference time.
    2. ``model_info["<arch>.context_length"]`` — architecture's
       natural ceiling. Ollama exposes this for known
       architectures. The model COULD use this much context if
       the operator raised num_ctx; it's the upper bound.
    3. The 2048 default. Ollama's documented behaviour when no
       num_ctx is set and the architecture doesn't expose a
       context_length. WARNING-logged because operators rarely
       intend this.
    """

    backend: Backend = "ollama"

    async def discover(
        self,
        model: ModelConfig,
        secrets: Secrets | None,
        *,
        timeout_s: float,
    ) -> ContextWindowResult:
        ts = time.time()
        if not model.endpoint:
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail="ollama model has no endpoint configured",
                discovered_at=ts,
            )

        url = model.endpoint.rstrip("/") + "/api/show"
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as http:
                r = await http.post(url, json={"name": model.model})
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPError as exc:
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"transport: {type(exc).__name__}: {exc}",
                discovered_at=ts,
            )
        except (ValueError, TypeError) as exc:  # malformed JSON
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"malformed response: {type(exc).__name__}: {exc}",
                discovered_at=ts,
            )

        # Layer 1: parameters text — look for ``num_ctx <N>``.
        # The ``parameters`` field is a multi-line string with
        # one ``key value`` pair per line.
        parameters = body.get("parameters")
        if isinstance(parameters, str):
            for line in parameters.splitlines():
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2 and parts[0] == "num_ctx":
                    try:
                        n = int(parts[1])
                    except ValueError:
                        continue
                    if n > 0:
                        return ContextWindowResult(
                            tokens=n,
                            source="modelfile",
                            detail="ollama num_ctx parameter set",
                            discovered_at=ts,
                        )

        # Layer 2: architecture's natural context length.
        # ``model_info`` is a dict with keys like
        # ``general.architecture: "qwen2"`` and
        # ``qwen2.context_length: 32768``.
        model_info = body.get("model_info")
        if isinstance(model_info, dict):
            arch = model_info.get("general.architecture")
            if isinstance(arch, str):
                key = f"{arch}.context_length"
                value = model_info.get(key)
                if isinstance(value, int) and value > 0:
                    return ContextWindowResult(
                        tokens=value,
                        source="model_info",
                        detail=(f"ollama architecture {arch!r} natural context_length"),
                        discovered_at=ts,
                    )

        # Layer 3: fall back to the documented default. Loud
        # warning — operators rarely intend 2048 in 2026.
        _log.warning(
            "context_window.ollama_default_fallback",
            extra={
                "model_id": model.id,
                "ollama_model": model.model,
                "endpoint": model.endpoint,
                "hint": (
                    "no num_ctx parameter and no model_info "
                    "context_length found; falling back to "
                    "Ollama's 2048 default. Set "
                    "OLLAMA_CONTEXT_LENGTH on the satellite or "
                    "add a per-model num_ctx override to raise "
                    "it."
                ),
            },
        )
        return ContextWindowResult(
            tokens=_OLLAMA_DEFAULT_CONTEXT,
            source="default",
            detail="ollama documented default (no num_ctx, no model_info)",
            discovered_at=ts,
        )


# --------------------------------------------------------------- openai-shape


class OpenAIContextProbe:
    """Discover via ``GET /v1/models``.

    Covers the FITT ``openai`` backend (any OpenAI-compatible
    endpoint we don't have a dedicated backend for) and the
    ``openrouter`` backend (which OpenRouter renames
    ``context_length`` to ``max_input_tokens`` but otherwise
    matches the OpenAI shape).

    Some providers don't expose ``/v1/models`` at all (NVIDIA's
    NIM endpoints, occasionally), in which case we degrade to
    ``tokens=None`` rather than guessing.
    """

    backend: Backend  # set on instance

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    async def discover(
        self,
        model: ModelConfig,
        secrets: Secrets | None,
        *,
        timeout_s: float,
    ) -> ContextWindowResult:
        ts = time.time()

        # Build the endpoint URL. ``openai`` backend: per-model
        # endpoint. ``openrouter`` backend: a fixed public
        # endpoint LiteLLM also uses.
        if self.backend == "openrouter":
            url = "https://openrouter.ai/api/v1/models"
        elif self.backend == "openai":
            if not model.endpoint:
                return ContextWindowResult(
                    tokens=None,
                    source="unknown",
                    detail="openai-shape model has no endpoint configured",
                    discovered_at=ts,
                )
            url = model.endpoint.rstrip("/") + "/v1/models"
        else:
            # Defensive — caller shouldn't dispatch this probe
            # for backends it doesn't handle.
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"OpenAIContextProbe got unsupported backend {self.backend!r}",
                discovered_at=ts,
            )

        api_key = (
            secrets.api_key_for(self.backend, model_id=model.id) if secrets is not None else None
        )
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=timeout_s) as http:
                r = await http.get(url, headers=headers)
                r.raise_for_status()
                body = r.json()
        except httpx.HTTPError as exc:
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"transport: {type(exc).__name__}: {exc}",
                discovered_at=ts,
            )
        except (ValueError, TypeError) as exc:
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"malformed response: {type(exc).__name__}: {exc}",
                discovered_at=ts,
            )

        # OpenAI shape: ``{"object": "list", "data": [...]}``.
        # Each entry has ``id`` plus provider-specific fields.
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail="response missing 'data' list",
                discovered_at=ts,
            )

        match = next(
            (entry for entry in data if isinstance(entry, dict) and entry.get("id") == model.model),
            None,
        )
        if match is None:
            return ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"model {model.model!r} not in /v1/models",
                discovered_at=ts,
            )

        # Try common field names in priority order. OpenRouter
        # uses ``context_length``; NIM and Groq mostly use the
        # same; some legacy responses use ``max_input_tokens``.
        for field in ("context_length", "max_input_tokens"):
            value = match.get(field)
            if isinstance(value, int) and value > 0:
                return ContextWindowResult(
                    tokens=value,
                    source="api_models",
                    detail=f"/v1/models[id={model.model!r}].{field}",
                    discovered_at=ts,
                )

        return ContextWindowResult(
            tokens=None,
            source="unknown",
            detail=f"/v1/models[id={model.model!r}] missing context_length and max_input_tokens",
            discovered_at=ts,
        )


# --------------------------------------------------------------- anthropic


class AnthropicContextProbe:
    """Static lookup table.

    Anthropic doesn't expose context length on
    ``/v1/models`` (and the lifecycle of dedicated public
    metadata endpoints is unclear). The lookup table sits
    in ``_ANTHROPIC_CONTEXT_BY_FAMILY``; adding a new
    Anthropic generation is a one-line edit.

    Match is by family prefix — so ``claude-sonnet-4-5``
    matches the ``claude-sonnet-4`` family entry too. Most
    specific match wins, so the table can carry both
    family-level and version-specific entries when they
    differ.
    """

    backend: Backend = "anthropic"

    async def discover(
        self,
        model: ModelConfig,
        secrets: Secrets | None,
        *,
        timeout_s: float,
    ) -> ContextWindowResult:
        ts = time.time()
        ident = model.model.lower()

        # Most specific match wins — sort families by length
        # descending so ``claude-sonnet-4-5`` beats
        # ``claude-sonnet-4``.
        sorted_families = sorted(
            _ANTHROPIC_CONTEXT_BY_FAMILY.items(),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )
        for family, tokens in sorted_families:
            if ident.startswith(family):
                return ContextWindowResult(
                    tokens=tokens,
                    source="lookup_table",
                    detail=f"anthropic family match: {family!r}",
                    discovered_at=ts,
                )

        return ContextWindowResult(
            tokens=None,
            source="unknown",
            detail=(
                f"anthropic model {model.model!r} not in lookup table; "
                "add to _ANTHROPIC_CONTEXT_BY_FAMILY in context_window.py"
            ),
            discovered_at=ts,
        )


# --------------------------------------------------------------- cache


class ContextWindowCache:
    """In-memory cache of per-binding discovery results.

    Lifetime: one gateway process. Refresh at boot in
    :meth:`populate` and on operator command via
    :meth:`refresh_one`. Lookups in :meth:`get` are
    synchronous; the chat handler reads them on every dispatch
    so they need to be fast and never block.

    Backend dispatch picks the right probe by ``model.backend``.
    Adding a new backend is two changes: add the probe class
    above, and extend ``_PROBE_FACTORY`` below.
    """

    def __init__(self) -> None:
        # Keyed by ``(backend, model_id)``; the same model_id can
        # only appear once in config.yaml, so this is unique by
        # construction.
        self._results: dict[tuple[Backend, str], ContextWindowResult] = {}
        self._probes: dict[Backend, ContextWindowProbe] = {
            "ollama": OllamaContextProbe(),
            "openai": OpenAIContextProbe("openai"),
            "openrouter": OpenAIContextProbe("openrouter"),
            "anthropic": AnthropicContextProbe(),
        }

    def get(
        self,
        backend: Backend,
        model_id: str,
    ) -> ContextWindowResult | None:
        """Return the cached result for a binding, or ``None`` if
        discovery hasn't run for it yet."""
        return self._results.get((backend, model_id))

    async def populate(
        self,
        config: Config,
        *,
        timeout_s: float = 5.0,
    ) -> None:
        """Discover context windows for every model in ``config``.

        Concurrent across models — different backends don't
        contend, and concurrent probes against the same backend
        are bounded by the model count. Total boot cost stays
        well under ``timeout_s * 2`` for typical 2-5 alias
        configs.

        Failures don't propagate; each binding's result lands
        in the cache regardless. ERROR-level log per failure
        with the alias / backend / detail (Principle 11
        shape)."""
        secrets = config.secrets

        async def probe_one(
            model: ModelConfig,
        ) -> tuple[tuple[Backend, str], ContextWindowResult]:
            probe = self._probes.get(model.backend)
            if probe is None:
                return (
                    (model.backend, model.id),
                    ContextWindowResult(
                        tokens=None,
                        source="unknown",
                        detail=f"no probe registered for backend {model.backend!r}",
                        discovered_at=time.time(),
                    ),
                )
            try:
                result = await probe.discover(model, secrets, timeout_s=timeout_s)
            except Exception as exc:
                # Probe contract says don't raise. If a probe
                # does anyway, log and recover — never let a
                # discovery bug crash gateway boot.
                _log.error(
                    "context_window.probe_raised",
                    extra={
                        "model_id": model.id,
                        "backend": model.backend,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                result = ContextWindowResult(
                    tokens=None,
                    source="unknown",
                    detail=f"probe raised: {type(exc).__name__}: {exc}",
                    discovered_at=time.time(),
                )
            return ((model.backend, model.id), result)

        pairs = await asyncio.gather(*(probe_one(m) for m in config.models))
        for key, result in pairs:
            self._results[key] = result
            backend, model_id = key
            if result.tokens is None or result.source in ("unknown", "default"):
                # Loud at ERROR for unknowns (Principle 11) and
                # at WARNING for the Ollama 2048 fallback (the
                # OllamaContextProbe already logged its own
                # warning, but log again here so the cache
                # state is summarised in one line per binding).
                level = logging.ERROR if result.source == "unknown" else logging.INFO
                _log.log(
                    level,
                    "context_window.discovered",
                    extra={
                        "backend": backend,
                        "model_id": model_id,
                        "tokens": result.tokens,
                        "source": result.source,
                        "detail": result.detail,
                    },
                )
            else:
                _log.info(
                    "context_window.discovered",
                    extra={
                        "backend": backend,
                        "model_id": model_id,
                        "tokens": result.tokens,
                        "source": result.source,
                    },
                )

    async def refresh_one(
        self,
        config: Config,
        model_id: str,
        *,
        timeout_s: float = 5.0,
    ) -> ContextWindowResult:
        """Re-run discovery for one model. Used by the
        ``fitt context refresh`` CLI / endpoint when an operator
        has changed the backend's config (e.g. raised Ollama's
        ``OLLAMA_CONTEXT_LENGTH``) and wants the gateway to pick
        it up without a process restart.

        Raises ``KeyError`` if ``model_id`` isn't in the config —
        callers translate to a 404 / clear CLI message.
        """
        model = next((m for m in config.models if m.id == model_id), None)
        if model is None:
            raise KeyError(model_id)
        probe = self._probes.get(model.backend)
        if probe is None:
            result = ContextWindowResult(
                tokens=None,
                source="unknown",
                detail=f"no probe registered for backend {model.backend!r}",
                discovered_at=time.time(),
            )
        else:
            try:
                result = await probe.discover(model, config.secrets, timeout_s=timeout_s)
            except Exception as exc:
                _log.error(
                    "context_window.probe_raised",
                    extra={
                        "model_id": model.id,
                        "backend": model.backend,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                result = ContextWindowResult(
                    tokens=None,
                    source="unknown",
                    detail=f"probe raised: {type(exc).__name__}: {exc}",
                    discovered_at=time.time(),
                )
        self._results[(model.backend, model.id)] = result
        return result

    def all_results(self) -> dict[tuple[Backend, str], ContextWindowResult]:
        """Return a copy of every cached result. Used by
        ``GET /v1/aliases`` to render every binding's window in
        one shot, and by ``fitt context list``."""
        return dict(self._results)

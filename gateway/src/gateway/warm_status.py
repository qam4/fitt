"""Ollama warm-state (VRAM residency) helpers.

Ollama's ``GET /api/ps`` lists the models currently loaded in VRAM on a
host — the "what's warm right now" signal. This matters for eval
hygiene: two big models co-resident on one GPU contend for VRAM and can
force CPU offload, which silently pollutes latency (and can make a
capable model look terrible — observed with gemma4 co-resident with
qwen3, 2026-08-10). These helpers let a caller see what's warm and
evict co-resident models (``keep_alive: 0``) so a measurement runs
against a single, fully-resident model.

Endpoint-scoped by construction: ``/api/ps`` reports one host, so
"co-resident" means "loaded on the same endpoint as the DUT".

Deployment-neutral and injectable: pass an ``httpx.AsyncClient`` (tests
use a fake transport); nothing here reads global state.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

DEFAULT_PS_TIMEOUT_S = 5.0
DEFAULT_UNLOAD_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class LoadedModel:
    """One entry from ``/api/ps`` — a model resident in VRAM."""

    name: str
    size_vram: int  # bytes resident in VRAM (0 == fully on CPU)
    context_length: int | None  # the num_ctx it was loaded with


def _parse_ps(payload: dict) -> list[LoadedModel]:  # type: ignore[type-arg]
    out: list[LoadedModel] = []
    for m in payload.get("models", []):
        if not isinstance(m, dict):
            continue
        out.append(
            LoadedModel(
                name=str(m.get("name") or m.get("model") or "?"),
                size_vram=int(m.get("size_vram", 0) or 0),
                context_length=(
                    int(m["context_length"]) if isinstance(m.get("context_length"), int) else None
                ),
            )
        )
    return out


async def list_loaded(
    endpoint: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_PS_TIMEOUT_S,
) -> list[LoadedModel]:
    """Return the models currently loaded in VRAM on ``endpoint``.

    Never raises — a transport error or non-ollama endpoint yields an
    empty list (the caller treats "can't tell" as "nothing known warm")."""
    url = f"{endpoint.rstrip('/')}/api/ps"

    async def _do(c: httpx.AsyncClient) -> list[LoadedModel]:
        try:
            r = await c.get(url)
            if r.status_code >= 400:
                return []
            return _parse_ps(r.json())
        except (httpx.RequestError, ValueError):
            return []

    if client is not None:
        return await _do(client)
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        return await _do(c)


async def unload(
    endpoint: str,
    model: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_UNLOAD_TIMEOUT_S,
) -> bool:
    """Ask ollama to evict ``model`` from VRAM now (``keep_alive: 0``).

    Uses ``/api/chat`` with an empty message list, which ollama treats
    as a load/unload control call. Returns True on a 2xx, False
    otherwise. Never raises."""
    url = f"{endpoint.rstrip('/')}/api/chat"
    body = {"model": model, "messages": [], "keep_alive": 0}

    async def _do(c: httpx.AsyncClient) -> bool:
        try:
            r = await c.post(url, json=body)
            return r.status_code < 400
        except httpx.RequestError:
            return False

    if client is not None:
        return await _do(client)
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        return await _do(c)


async def evict_others(
    endpoint: str,
    keep_model: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_UNLOAD_TIMEOUT_S,
) -> list[str]:
    """Unload every VRAM-resident model on ``endpoint`` except
    ``keep_model``. Returns the names it evicted (best-effort)."""
    loaded = await list_loaded(endpoint, client=client)
    evicted: list[str] = []
    for m in loaded:
        if m.name == keep_model:
            continue
        if await unload(endpoint, m.name, client=client, timeout_s=timeout_s):
            evicted.append(m.name)
    return evicted


# --------------------------------------------------------------- template check

DEFAULT_SHOW_TIMEOUT_S = 20.0


@dataclass(frozen=True, slots=True)
class TemplateCheck:
    """Whether a model's chat template can actually carry a tool loop.

    ``capabilities`` is what the model *claims*; ``renders_messages`` /
    ``mentions_tools`` are what its template can mechanically *do*. When
    a model claims tools but its template does neither, tool results can
    never reach it — it will re-emit the same call until the loop cap
    (observed with gemma4:12b-it-qat, whose template is the 13-character
    stub ``{{ .Prompt }}``, 2026-08-10)."""

    model: str
    template_len: int
    renders_messages: bool
    mentions_tools: bool
    claims_tools: bool
    detail: str | None = None

    @property
    def tool_capable(self) -> bool:
        """Can this template mechanically carry a tool exchange?"""
        return self.renders_messages and self.mentions_tools

    @property
    def mismatch(self) -> bool:
        """Claims tool support its template can't deliver — the
        dangerous case, because declared capabilities look fine."""
        return self.claims_tools and not self.tool_capable


async def check_template(
    endpoint: str,
    model: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = DEFAULT_SHOW_TIMEOUT_S,
) -> TemplateCheck:
    """Inspect ``model``'s chat template via ollama ``/api/show``.

    A cheap, no-inference pre-flight: strictly more trustworthy than the
    declared ``capabilities`` list, which can advertise ``tools`` for a
    model whose template cannot render them. Never raises — an
    unreachable host yields a check with ``detail`` set and everything
    False (the caller decides whether "can't tell" blocks a run)."""
    url = f"{endpoint.rstrip('/')}/api/show"

    async def _do(c: httpx.AsyncClient) -> TemplateCheck:
        try:
            r = await c.post(url, json={"model": model})
            if r.status_code >= 400:
                return TemplateCheck(model, 0, False, False, False, detail=f"HTTP {r.status_code}")
            data = r.json()
        except (httpx.RequestError, ValueError) as e:
            return TemplateCheck(model, 0, False, False, False, detail=str(e))
        template = str(data.get("template") or "")
        caps = data.get("capabilities") or []
        lowered = template.lower()
        return TemplateCheck(
            model=model,
            template_len=len(template),
            renders_messages=".messages" in lowered,
            mentions_tools="tool" in lowered,
            claims_tools=any(str(c).lower() == "tools" for c in caps),
        )

    if client is not None:
        return await _do(client)
    async with httpx.AsyncClient(timeout=timeout_s) as c:
        return await _do(c)

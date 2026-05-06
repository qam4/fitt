"""HTTP endpoints for inspecting and managing MCP servers.

Two endpoints:

* ``GET /v1/mcp`` — returns the MCPManager's summary: configured
  servers, whether each is running, and the tool names each
  currently has registered. Consumed by ``fitt mcp list``.
* ``POST /v1/mcp/{name}/restart`` — stop + start the named
  server. Consumed by ``fitt mcp restart <name>``.

Both are Bearer-auth'd (so the existing AuthMiddleware applies)
and not in the exempt-prefix list. Phase 4 doesn't add a
client-tag check; any auth'd caller can manage MCP. That can
tighten later (e.g. block from ``webui``) via a per-endpoint
client check if the threat model justifies it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


@router.get("/v1/mcp")
async def mcp_list(request: Request) -> dict[str, Any]:
    """Return ``{servers: [...]}``. Each server is summarised with
    ``name``, ``running``, ``command``, and the list of
    currently-registered tool names."""
    manager = request.app.state.mcp
    return {"servers": manager.describe()}


@router.post("/v1/mcp/{name}/restart")
async def mcp_restart(name: str, request: Request) -> dict[str, Any]:
    """Stop + start the named server."""
    manager = request.app.state.mcp
    registry = request.app.state.tool_registry
    try:
        await manager.restart(name, registry)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no MCP server named {name!r}") from None
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"restart failed: {e}") from None
    return {"ok": True, "server": name}

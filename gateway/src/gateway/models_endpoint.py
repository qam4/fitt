"""GET /v1/models — list configured aliases.

Shape mimics OpenAI's /v1/models response so any OpenAI-compatible
client (Continue, Cursor, Open WebUI, curl) shows the aliases in its
model picker.

Auth-exempt: this is a discovery endpoint. The actual /v1/chat/
completions endpoint still requires the Bearer token.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    config = request.app.state.config
    created = int(time.time())
    data = []
    for alias in config.alias_names():
        chain = config.resolve_alias(alias)
        primary = chain[0]
        data.append(
            {
                "id": alias,
                "object": "model",
                "created": created,
                "owned_by": "fitt",
                # Non-OpenAI extensions (still valid JSON, clients ignore
                # unknown keys).
                "fitt_backend": primary.backend,
                "fitt_resolved_model": primary.model,
                "fitt_fallback": primary.fallback,
            }
        )
    return {"object": "list", "data": data}

"""FastAPI application factory.

Keeping this small and composable so tests can build a gateway with
selected middleware without touching the rest of the stack.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .auth import AuthMiddleware
from .config import Config
from .errors import ModelIdNotAlias, NoBackendAvailable, UnknownAlias


def create_app(config: Config) -> FastAPI:
    """Build the gateway FastAPI app.

    Routes are registered later (health, models, chat) as each submodule
    is implemented. This function is what both production (``__main__``)
    and tests use.
    """
    app = FastAPI(
        title="FITT Gateway",
        version=__version__,
        # We use our own OpenAI-compatible schema; FastAPI's auto-docs
        # can still be useful during development.
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.config = config

    # Middleware registration order matters: auth runs first (outermost).
    app.add_middleware(AuthMiddleware, config=config)

    # Domain-error handlers. Must map to the HTTP status codes the
    # chat endpoint's documented contract specifies.

    @app.exception_handler(UnknownAlias)
    async def _handle_unknown_alias(_: Request, exc: UnknownAlias) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "unknown_alias",
                    "message": str(exc),
                    "available": exc.available,
                }
            },
        )

    @app.exception_handler(ModelIdNotAlias)
    async def _handle_model_id(_: Request, exc: ModelIdNotAlias) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "model_id_not_alias",
                    "message": str(exc),
                    "available": exc.available_aliases,
                }
            },
        )

    @app.exception_handler(NoBackendAvailable)
    async def _handle_no_backend(_: Request, exc: NoBackendAvailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "no_backend_available",
                    "message": str(exc),
                    "attempted": exc.attempted,
                }
            },
        )

    # Routers — imported lazily to keep import graph acyclic.
    from .health import router as health_router
    from .models_endpoint import router as models_router

    app.include_router(health_router)
    app.include_router(models_router)

    # Chat router is imported here once implemented.
    try:
        from .chat import router as chat_router

        app.include_router(chat_router)
    except ImportError:
        pass

    return app

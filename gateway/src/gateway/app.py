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
from .errors import (
    ModelIdNotAlias,
    NoBackendAvailable,
    UnknownAlias,
    UnknownSession,
)
from .memory import MemoryStore
from .sessions import SessionRegistry


def create_app(config: Config) -> FastAPI:
    """Build the gateway FastAPI app.

    Routes are registered here (health, models, chat). This factory
    is what both production (``__main__``) and tests use.
    """
    app = FastAPI(
        title="FITT Gateway",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.config = config

    # Memory store lives for the lifetime of the app. It reads the
    # identity files fresh on every request, so editing them takes
    # effect without a restart.
    app.state.memory = MemoryStore(
        identity_dir=config.memory.identity_dir,
        sessions_dir=config.memory.sessions_dir,
        max_history_chars=config.memory.max_history_chars,
        enabled=config.memory.enabled,
    )

    # Session registry: same freshness guarantee. `fitt session new`
    # from a separate shell is visible on the next request.
    app.state.session_registry = SessionRegistry(config.memory.sessions_dir)
    app.state.session_registry.ensure_main()

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

    @app.exception_handler(UnknownSession)
    async def _handle_unknown_session(_: Request, exc: UnknownSession) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "unknown_session",
                    "message": str(exc),
                    "available": exc.available,
                }
            },
        )

    # Routers - imported lazily to keep import graph acyclic.
    from .health import router as health_router
    from .models_endpoint import router as models_router

    app.include_router(health_router)
    app.include_router(models_router)

    try:
        from .chat import router as chat_router

        app.include_router(chat_router)
    except ImportError:
        pass

    return app

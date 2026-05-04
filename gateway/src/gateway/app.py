"""FastAPI application factory.

Keeping this small and composable so tests can build a gateway with
selected middleware without touching the rest of the stack.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .auth import AuthMiddleware
from .config import Config, default_config_path, default_secrets_path, load_config
from .errors import (
    ModelIdNotAlias,
    NoBackendAvailable,
    UnknownAlias,
    UnknownSession,
)
from .logging_config import configure_logging
from .memory import MemoryStore
from .sessions import SessionRegistry


def create_app_from_env() -> FastAPI:
    """Zero-arg factory for ``uvicorn --factory`` / ``--reload``.

    Loads config + secrets from the default paths (honours
    ``FITT_CONFIG_PATH`` / ``FITT_SECRETS_PATH`` / ``FITT_HOME``),
    configures logging, and returns a ready-to-serve app. Used by
    the docker-compose dev overlay so edits to source trigger a
    uvicorn reload without re-parsing argv.
    """
    cfg = load_config(default_config_path(), default_secrets_path())
    configure_logging(
        cfg.logging.dir,
        level=cfg.server.log_level,
        retention_days=cfg.logging.retention_days,
    )
    return create_app(cfg)


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

    # Tool subsystem (Phase 4). Registries + backend + approval
    # middleware all live for the lifetime of the app.
    #   - ProjectRegistry is re-read on every get() (like sessions),
    #     so editing $FITT_HOME/projects.yaml from the CLI is
    #     visible without a restart.
    #   - ExecutionBackend is stateless apart from the resolved
    #     SSH key path (discovered once from $FITT_HOME/ssh/).
    #   - ToolRegistry is an in-memory set; tool policy from
    #     config.yaml's `tools:` section (optional) gets parsed
    #     and attached here.
    #   - ApprovalMiddleware wraps the registry's policy ladder
    #     and will grow deny-list / audit / ask-UI hooks in
    #     later tasks.
    from .approval import ApprovalMiddleware
    from .projects import ProjectRegistry, default_projects_path
    from .tools import (
        ExecutionBackend,
        ToolPolicy,
        ToolRegistry,
        build_fileops_tools,
        build_git_tools,
        build_inline_tools,
    )

    app.state.project_registry = ProjectRegistry(default_projects_path())
    app.state.execution_backend = ExecutionBackend()

    tool_policy = ToolPolicy.from_config(config.tools)
    tool_registry = ToolRegistry(tool_policy)
    # Build and register each inline tool group in a stable order.
    # `build_inline_tools(registry)` captures the registry in a
    # closure for `list_capabilities`, so construct it before
    # registering anything else.
    for t in build_inline_tools(tool_registry):
        tool_registry.register(t)
    for t in build_fileops_tools():
        tool_registry.register(t)
    for t in build_git_tools():
        tool_registry.register(t)
    app.state.tool_registry = tool_registry
    app.state.approval = ApprovalMiddleware(tool_registry)

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

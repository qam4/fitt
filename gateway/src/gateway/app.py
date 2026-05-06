"""FastAPI application factory.

Keeping this small and composable so tests can build a gateway with
selected middleware without touching the rest of the stack.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .auth import AuthMiddleware
from .config import Config, default_config_path, default_secrets_path, fitt_home, load_config
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
    from .ssh_identity import default_key_path, ensure_key
    from .tools import (
        ExecutionBackend,
        ToolPolicy,
        ToolRegistry,
        build_cron_tools,
        build_fileops_tools,
        build_git_tools,
        build_inline_tools,
        build_shell_tools,
    )

    app.state.project_registry = ProjectRegistry(default_projects_path())

    # Generate the gateway's SSH identity on first boot. Idempotent:
    # existing keys are preserved; only missing ones are created.
    # Without a key, tools that need to reach satellites fail with
    # "ssh: Could not resolve hostname / permission denied" on
    # first use, which is easy to mistake for a config problem.
    # Doing it here moves the "wait, how do I make a key" step out
    # of the operator's setup path.
    ssh_key = default_key_path()
    try:
        # create_app is synchronous; run the async ssh-keygen wrapper
        # in a private event loop. Only runs once per process start.
        import asyncio

        asyncio.run(ensure_key(ssh_key))
        app.state.ssh_key_path = ssh_key
    except (FileNotFoundError, RuntimeError) as exc:
        # ssh-keygen missing (Dockerfile regression) or keygen
        # itself failed. Log loudly but don't block startup -
        # the chat path works without tools, and the operator
        # can fix things by running `fitt ssh pubkey` later
        # (which re-triggers the same code path).
        import logging as _stdlog

        _stdlog.getLogger("fitt.gateway").warning(
            "ssh.identity.unavailable", extra={"error": str(exc)}
        )
        app.state.ssh_key_path = None

    app.state.execution_backend = ExecutionBackend(ssh_key_path=app.state.ssh_key_path)

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
    for t in build_shell_tools():
        tool_registry.register(t)
    for t in build_cron_tools():
        tool_registry.register(t)
    app.state.tool_registry = tool_registry
    if tool_policy.approval_timeout_secs is not None:
        app.state.approval = ApprovalMiddleware(
            tool_registry,
            approval_timeout_s=tool_policy.approval_timeout_secs,
        )
    else:
        app.state.approval = ApprovalMiddleware(tool_registry)

    # Audit log: one AuditLog per gateway process, chained to
    # $FITT_HOME/audit.jsonl with a key at audit.key. Lazy key
    # generation: the key file is created on first append, not
    # at startup, so a gateway that never serves a tool call
    # doesn't leave a stray key file behind.
    from .audit import AuditLog, default_audit_paths

    audit_path, audit_key_path = default_audit_paths(fitt_home())
    app.state.audit = AuditLog(path=audit_path, key_path=audit_key_path)

    # Event log (Phase 4.5): append-only user-visible activity.
    # Distinct from audit (coarse, no HMAC, pruned). Feeds the
    # Telegram push channel and the `fitt inbox` CLI. See
    # `.kiro/specs/phase4.5-cron-events/design.md` ("Three logs,
    # three jobs") for the boundary.
    from .events import EventLog, default_events_path

    app.state.events = EventLog(default_events_path(fitt_home()))

    # Cron service (Phase 4.5): persistent cron jobs backed by
    # $FITT_HOME/cron.json. The scheduler loop that actually
    # fires due jobs lands in Phase 4.5 Task 4; this binding is
    # what the `cron_*` inline tools read so an operator can
    # create/list/remove crons today even without the loop.
    from .cron import CronService, default_cron_path

    app.state.cron = CronService(default_cron_path(fitt_home()))

    # Capability-gap log: append-only record of "I'd need a tool
    # to X" statements from the model. Ranked by `fitt
    # capability-gaps` into a prioritised list of tools-to-add.
    from .capabilities import CapabilityGapLog, default_gap_log_path

    app.state.capability_gaps = CapabilityGapLog(default_gap_log_path(fitt_home()))

    # MCP: parse the mcp_servers config block once at startup
    # and attach an MCPManager. The manager only actually spawns
    # subprocesses when the app's lifespan starts (so tests that
    # instantiate the app without entering its lifespan don't
    # spawn real processes). Invalid entries log a warning and
    # are skipped; one bad server shouldn't block the gateway.
    from .mcp import MCPManager, MCPServerConfig

    mcp_configs: list[MCPServerConfig] = []
    for raw in config.mcp_servers or []:
        try:
            mcp_configs.append(MCPServerConfig(**raw))
        except Exception as e:
            import logging as _stdlog

            _stdlog.getLogger("fitt.gateway").warning(
                "mcp.config_invalid",
                extra={"entry": raw, "error": str(e)},
            )
    app.state.mcp = MCPManager(configs=mcp_configs)

    # MCP lifespan: spawn servers on startup, tear down on
    # shutdown. We use on_event (still supported in FastAPI, and
    # simpler than migrating create_app to a full lifespan
    # context manager for one concern). Startup failures log
    # loudly but don't crash the app — the chat path works
    # without MCP tools.
    @app.on_event("startup")
    async def _start_mcp() -> None:  # pragma: no cover - lifespan hook
        if not mcp_configs:
            return
        import logging as _stdlog

        _stdlog.getLogger("fitt.gateway").info(
            "mcp.starting",
            extra={"count": len(mcp_configs)},
        )
        await app.state.mcp.start_all(app.state.tool_registry)

    @app.on_event("shutdown")
    async def _stop_mcp() -> None:  # pragma: no cover - lifespan hook
        await app.state.mcp.stop_all()

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
    from .approvals_endpoint import router as approvals_router
    from .health import router as health_router
    from .mcp_endpoint import router as mcp_router
    from .models_endpoint import router as models_router

    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(approvals_router)
    app.include_router(mcp_router)

    try:
        from .chat import router as chat_router

        app.include_router(chat_router)
    except ImportError:
        pass

    return app

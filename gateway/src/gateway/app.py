"""FastAPI application factory.

Keeping this small and composable so tests can build a gateway with
selected middleware without touching the rest of the stack.
"""

from __future__ import annotations

import os

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
from .logging_config import configure_logging, get_logger
from .memory import MemoryStore
from .request_id import RequestIdMiddleware
from .sessions import SessionRegistry

_log = get_logger("fitt.gateway.app")


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

    # Principle 11: fail loud on detectable misconfigurations.
    # At startup, walk the config + secrets pair and emit an
    # ERROR log for any openai-backend model missing its
    # api_keys entry. Non-fatal — other aliases may work fine,
    # and refusing to start on one misconfigured entry would
    # make things worse. See docs/observed-issues.md for the
    # failure mode this catches.
    from .config import check_missing_api_keys

    for warning in check_missing_api_keys(config):
        _log.error("config.missing_api_key", extra={"detail": warning})

    # Memory store lives for the lifetime of the app. It reads the
    # identity files fresh on every request, so editing them takes
    # effect without a restart.
    #
    # Phase 5: a LessonsStore rides alongside the identity files at
    # ``$FITT_HOME/identity/lessons.md``. Auto-mutated by the
    # ``learn_*`` tools, hand-editable by operators. Injected into
    # every request as a ``[Learned corrections]`` block after the
    # identity content.
    from .lessons import LessonsStore, default_lessons_path

    memory_cfg = config.memory
    lessons_store = LessonsStore(
        default_lessons_path(memory_cfg.identity_dir),
        max_entries=getattr(memory_cfg, "max_lessons", 50),
    )
    app.state.lessons = lessons_store

    app.state.memory = MemoryStore(
        identity_dir=memory_cfg.identity_dir,
        sessions_dir=memory_cfg.sessions_dir,
        max_history_chars=memory_cfg.max_history_chars,
        enabled=memory_cfg.enabled,
        lessons=lessons_store,
    )

    # Phase 4.10 — skills loader. Walks ``skills_dir`` once at
    # boot, parses every ``SKILL.md``, and caches the result on
    # ``app.state.skills``. The chat handler renders the
    # ``[Skills available]`` system-prompt block from this list
    # on every request. Edit-and-restart contract: changes to
    # SKILL.md files don't take effect until the next gateway
    # boot (matches identity.md). Never raises out of scan().
    from .skills import SkillsLoader

    skills_loader = SkillsLoader(
        skills_dir=memory_cfg.skills_dir,
        enabled=memory_cfg.skills_enabled,
    )
    app.state.skills = skills_loader.scan()

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
        ApprovalBucket,
        ExecutionBackend,
        SendMessageRateLimiter,
        ToolPolicy,
        ToolRegistry,
        build_cron_tools,
        build_fileops_tools,
        build_git_tools,
        build_inline_tools,
        build_lessons_tools,
        build_project_shell_tool,
        build_send_message_tool,
        build_shell_tools,
        build_web_search_tools,
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

    # Phase 4.7: resolve the local POSIX-shell interpreter at
    # startup so shell-requiring tools don't discover "no bash
    # on this Windows hub" on the first live request. The
    # probe caches its result; later calls are free. An
    # unresolvable hub (no bash, no Git Bash, no WSL) logs a
    # WARNING here and attaches a ``label="none"`` interpreter —
    # ``project_shell``'s local path fails with a readable
    # error in that case; SSH-backed projects are unaffected.
    #
    # ``FITT_SKIP_SHELL_PROBE=1`` short-circuits the probe with
    # a ``none`` interpreter. Intended for tests that don't
    # exercise project_shell and shouldn't pay the 2s-ish
    # subprocess cost per create_app. Production should never
    # set this.
    from .tools.local_shell import LocalShellProbe, ShellInterpreter

    if os.environ.get("FITT_SKIP_SHELL_PROBE") == "1":
        app.state.local_shell = ShellInterpreter.none()
    else:
        _shell_probe = LocalShellProbe()
        try:
            import asyncio as _asyncio

            app.state.local_shell = _asyncio.run(_shell_probe.detect())
        except RuntimeError as exc:
            # ``asyncio.run`` complains if we're inside a
            # running loop. Fall back to ``none`` so create_app
            # stays synchronous and callable from any context.
            import logging as _stdlog

            _stdlog.getLogger("fitt.gateway").warning(
                "shell.probe_skipped",
                extra={"error": str(exc), "reason": "running in an event loop"},
            )
            app.state.local_shell = ShellInterpreter.none()

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
    # Phase 4.11 — web_search tool. Triggers provider discovery
    # as a side effect; the configured backend's name is baked
    # into the tool's description (cf. design.md Decision 5).
    for t in build_web_search_tools(config.web.search_backend):
        tool_registry.register(t)
    for t in build_cron_tools():
        tool_registry.register(t)
    for t in build_lessons_tools():
        tool_registry.register(t)

    # Phase 4.7: project_shell. Registered with baked-in per-
    # client defaults so Open WebUI (least-trust) gets ``block``
    # by default without operator config. Other clients ``ask``;
    # operators can tighten (to ``block``) or loosen (to
    # ``trust_session`` for the IDE flow) via ``tools.per_client``
    # in config.yaml — baked defaults sit BELOW operator config
    # in the resolve chain.
    tool_registry.register(
        build_project_shell_tool(),
        per_client_defaults={
            "cli": ApprovalBucket.ASK,
            "telegram": ApprovalBucket.ASK,
            "ide": ApprovalBucket.ASK,
            "webui": ApprovalBucket.BLOCK,
        },
    )

    # Phase 4.5 Task 6: send_message. The rate limiter lives for
    # the gateway's lifetime (in-memory; a restart resets
    # counts, matching the approval middleware's posture). The
    # push-channel probe closes over ``config`` so a hot config
    # reload that adds/removes Telegram secrets propagates on
    # the next call — no restart needed.
    send_message_limiter = SendMessageRateLimiter(
        window_secs=60.0,
        max_per_window=10,
    )
    app.state.send_message_limiter = send_message_limiter

    def _has_push_channel() -> bool:
        """Heuristic mirror of chat.py's ``_push_channel_available``.
        Returns True when Telegram secrets are configured —
        best-effort signal that a subscriber is (or could be)
        running. Missing secrets means the event is only visible
        via ``fitt inbox``."""
        secrets = getattr(config, "secrets", None)
        if secrets is None:
            return False
        return getattr(secrets, "telegram", None) is not None

    tool_registry.register(
        build_send_message_tool(
            limiter=send_message_limiter,
            push_channel_available=_has_push_channel,
        )
    )
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

    # Tool-output artifact store (hallucinations doc item 4):
    # hoists over-threshold tool payloads to
    # $FITT_HOME/sessions/<key>/artifacts/<day>/<uuid>.txt so
    # they don't bloat the in-flight turn's context or linger
    # verbatim in tomorrow's history. Read by the agent loop
    # right before it builds the ``role: tool`` message.
    from .tool_artifacts import ArtifactStore

    app.state.artifact_store = ArtifactStore(
        sessions_dir=config.memory.sessions_dir,
        max_inline_bytes=config.memory.tool_output_max_inline_bytes,
        preview_bytes=config.memory.tool_output_preview_bytes,
    )

    # Per-turn event stream (Phase 4.8): fine-grained per-turn
    # detail — every LLM dispatch, every tool call, every
    # approval. Backs `fitt watch`, the HTTP /turns endpoints,
    # and the Telegram live-turn renderer (via 4.8c's SSE
    # bridge). JSONL at sessions/<key>/turns/<YYYY-MM-DD>.jsonl,
    # same retention as history.
    from .turns import TurnLog

    app.state.turns = TurnLog(sessions_dir=config.memory.sessions_dir)

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

    # Cron scheduler (Phase 4.5 task 4): ticks every
    # cron.poll_interval_secs, fires due jobs. The CronRunner
    # (task 5) spawns an agent session per firing using the
    # headless run_agent_loop, and emits cron_fired /
    # cron_completed / cron_failed events.
    from .cron_runner import CronRunner
    from .cron_scheduler import CronScheduler

    cron_runner = CronRunner(
        config=config,
        tool_registry=app.state.tool_registry,
        approval=app.state.approval,
        memory=app.state.memory,
        events=app.state.events,
        audit=app.state.audit,
        project_registry=app.state.project_registry,
        execution_backend=app.state.execution_backend,
        capability_gaps=app.state.capability_gaps,
        cron_service=app.state.cron,
        local_shell=app.state.local_shell,
        lessons=app.state.lessons,
        artifact_store=app.state.artifact_store,
        turns=app.state.turns,
    )
    app.state.cron_runner = cron_runner
    app.state.cron_scheduler = CronScheduler(app.state.cron, on_fire=cron_runner.fire)

    # Event pruner (Phase 4.5 Task 10): keeps events.jsonl
    # bounded without user-visible clutter in ``cron.json``.
    # See ``gateway/src/gateway/event_pruner.py`` for the "why
    # not a regular cron" discussion.
    from .event_pruner import EventPruner, default_anchor_path

    events_cfg = config.events or {}
    events_max_age_days = int(events_cfg.get("max_age_days", 90))
    app.state.event_pruner = EventPruner(
        events=app.state.events,
        max_age_days=events_max_age_days,
        anchor_path=default_anchor_path(fitt_home()),
    )

    @app.on_event("startup")
    async def _start_cron_scheduler() -> None:  # pragma: no cover - lifespan hook
        await app.state.cron_scheduler.start()

    @app.on_event("shutdown")
    async def _stop_cron_scheduler() -> None:  # pragma: no cover - lifespan hook
        await app.state.cron_scheduler.stop()

    @app.on_event("startup")
    async def _start_event_pruner() -> None:  # pragma: no cover - lifespan hook
        await app.state.event_pruner.start()

    @app.on_event("shutdown")
    async def _stop_event_pruner() -> None:  # pragma: no cover - lifespan hook
        await app.state.event_pruner.stop()

    # Phase 5 Task 9: history pruner. Same shape as the event
    # pruner (daily tick, anchor file, system_pruned event).
    # Walks sessions/*/history/*.md and deletes files older
    # than ``memory.history_max_days`` (default 90).
    from .history_pruner import HistoryPruner, default_history_anchor_path

    history_max_days = int(getattr(config.memory, "history_max_days", 90))
    app.state.history_pruner = HistoryPruner(
        sessions_dir=config.memory.sessions_dir,
        events=app.state.events,
        max_age_days=history_max_days,
        anchor_path=default_history_anchor_path(fitt_home()),
    )

    @app.on_event("startup")
    async def _start_history_pruner() -> None:  # pragma: no cover - lifespan hook
        await app.state.history_pruner.start()

    @app.on_event("shutdown")
    async def _stop_history_pruner() -> None:  # pragma: no cover - lifespan hook
        await app.state.history_pruner.stop()

    # Phase 7 Slice 7.1: per-binding context-window discovery.
    # Discovers each bound model's effective context window
    # at boot and caches the result for the lifetime of the
    # process. See gateway/context_window.py for per-backend
    # probe details. Best-effort — a probe failure stores
    # tokens=None for that binding but doesn't block startup.
    # Operators re-run discovery via `fitt context refresh`
    # without a process restart.
    from .context_window import ContextWindowCache

    app.state.context_windows = ContextWindowCache()

    @app.on_event("startup")
    async def _populate_context_windows() -> None:  # pragma: no cover - lifespan hook
        await app.state.context_windows.populate(
            config,
            timeout_s=config.server.context_probe_timeout_s,
        )

    # Boot-time alias tool-call reliability probe (Principle 11).
    # Fires one canary tool-call request per alias and logs an
    # ERROR per binding that narrates instead of emitting real
    # tool_calls. See gateway/alias_probe.py. Disabled via
    # ``server.boot_probe_enabled = false`` for tests that don't
    # want network traffic at startup.
    #
    # Phase 7 Slice 7.1: results also stash on
    # ``app.state.alias_probe_results`` (alias -> ProbeResult)
    # so the ``/v1/aliases`` endpoint can surface "last probe"
    # detail. Empty dict when probing is disabled or an
    # infrastructure failure prevented results landing.
    app.state.alias_probe_results = {}
    app.state.alias_probe_ran_at = None

    @app.on_event("startup")
    async def _run_boot_probe() -> None:  # pragma: no cover - lifespan hook
        if not config.server.boot_probe_enabled:
            return
        from .alias_probe import probe_all_aliases
        from .router import AliasRouter

        router = AliasRouter(config)
        try:
            results = await probe_all_aliases(
                config,
                router,
                timeout_s=config.server.boot_probe_timeout_s,
            )
        except Exception as exc:
            # Probe infrastructure itself failed. Log and move
            # on — we're a reliability check, not a load-bearing
            # component.
            _log.warning(
                "alias_probe.infrastructure_failure",
                extra={"error": f"{type(exc).__name__}: {exc}"},
            )
            return

        # Phase 7 Slice 7.1: persist results for the
        # ``/v1/aliases`` endpoint to surface.
        import time as _time

        app.state.alias_probe_ran_at = _time.time()
        app.state.alias_probe_results = {r.alias: r for r in results}

        for r in results:
            if r.status == "ok":
                _log.info(
                    "alias_probe.ok",
                    extra={
                        "alias": r.alias,
                        "model": r.model_used,
                        "detail": r.detail,
                    },
                )
            elif r.status == "skipped_no_api_key":
                # api_keys check already logged this — stay
                # quiet at DEBUG so we don't double-shout.
                _log.debug(
                    "alias_probe.skipped",
                    extra={"alias": r.alias, "detail": r.detail},
                )
            else:
                _log.error(
                    f"alias_probe.{r.status}",
                    extra={
                        "alias": r.alias,
                        "model": r.model_used,
                        "finish_reason": r.finish_reason,
                        "detail": r.detail,
                        "reply_preview": r.reply_preview,
                    },
                )

    # Middleware registration order matters: in Starlette,
    # the LAST middleware added wraps the others (outermost).
    # Auth runs *inside* the request-id wrapper so that every
    # request — even ones rejected with 401 — appears in
    # structured logs under a request_id; that's the whole
    # point of the wrapper, otherwise auth-rejected requests
    # disappear from the cross-log correlation story.
    app.add_middleware(AuthMiddleware, config=config)
    app.add_middleware(RequestIdMiddleware)

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
    from .aliases_endpoint import router as aliases_router
    from .approvals_endpoint import router as approvals_router
    from .events_endpoint import router as events_router
    from .health import router as health_router
    from .mcp_endpoint import router as mcp_router
    from .models_endpoint import router as models_router

    app.include_router(health_router)
    app.include_router(models_router)
    app.include_router(aliases_router)
    app.include_router(approvals_router)
    app.include_router(events_router)
    app.include_router(mcp_router)

    try:
        from .chat import router as chat_router

        app.include_router(chat_router)
    except ImportError:
        pass

    return app

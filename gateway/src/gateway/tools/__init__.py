"""Agentic tools subsystem.

The public surface is intentionally small at this stage of Phase 4:
type definitions and the registry. Tool implementations, the SSH
backend, and approval/audit pipelines land in later tasks and will
re-export from here once they're stable enough to commit to an API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import deny_list
from ._types import (
    ApprovalBucket,
    ApprovalDecision,
    Tool,
    ToolCallable,
    ToolContext,
    ToolResult,
)
from .backend import ExecutionBackend, ShellResult
from .cron_tools import build_cron_tools
from .fileops import build_fileops_tools
from .gitops import build_git_tools
from .inline import build_inline_tools
from .lessons import build_lessons_tools
from .plan_tools import build_plan_tools
from .project_shell import build_project_shell_tool
from .registry import ToolPolicy, ToolRegistry
from .retrieval_search import build_retrieval_tools
from .send_message import SendMessageRateLimiter, build_send_message_tool
from .shelltools import build_shell_tools
from .web_search import build_web_search_tools

if TYPE_CHECKING:
    from ..config import Config


def build_core_tool_registry(config: Config) -> ToolRegistry:
    """Assemble a :class:`ToolRegistry` with the dependency-free tool
    groups: inline, fileops, git, shell, web_search, cron, lessons, plan.

    This is everything :func:`gateway.app.create_app` registers *except*
    ``project_shell`` and ``send_message``, which need runtime handles
    (per-client approval defaults, the rate limiter + push-channel probe).
    ``create_app`` calls this and then adds those two; headless callers
    (the ``fitt eval`` CLI, and any other non-server path that needs the
    real tool schemas) use it directly, so the tool assembly lives in one
    place instead of being copied. The eval suites only name tools in this
    core set, so a registry built here offers the same real schemas the
    gateway ships."""
    registry = ToolRegistry(ToolPolicy.from_config(config.tools))
    for t in build_inline_tools(registry):
        registry.register(t)
    for t in build_fileops_tools():
        registry.register(t)
    for t in build_git_tools():
        registry.register(t)
    for t in build_shell_tools():
        registry.register(t)
    for t in build_web_search_tools(config.web.search_backend):
        registry.register(t)
    for t in build_cron_tools():
        registry.register(t)
    for t in build_lessons_tools():
        registry.register(t)
    for t in build_plan_tools():
        registry.register(t)
    # Phase 9d: memory_search is offered only when cross-session
    # retrieval is configured (memory.embedding_alias bound), so the
    # model never sees a dead tool on a retrieval-off deployment.
    if getattr(config.memory, "embedding_alias", None):
        for t in build_retrieval_tools():
            registry.register(t)
    return registry


__all__ = [
    "ApprovalBucket",
    "ApprovalDecision",
    "ExecutionBackend",
    "SendMessageRateLimiter",
    "ShellResult",
    "Tool",
    "ToolCallable",
    "ToolContext",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
    "build_core_tool_registry",
    "build_cron_tools",
    "build_fileops_tools",
    "build_git_tools",
    "build_inline_tools",
    "build_lessons_tools",
    "build_plan_tools",
    "build_project_shell_tool",
    "build_retrieval_tools",
    "build_send_message_tool",
    "build_shell_tools",
    "build_web_search_tools",
    "deny_list",
]

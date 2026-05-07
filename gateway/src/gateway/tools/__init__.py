"""Agentic tools subsystem.

The public surface is intentionally small at this stage of Phase 4:
type definitions and the registry. Tool implementations, the SSH
backend, and approval/audit pipelines land in later tasks and will
re-export from here once they're stable enough to commit to an API.
"""

from __future__ import annotations

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
from .registry import ToolPolicy, ToolRegistry
from .send_message import SendMessageRateLimiter, build_send_message_tool
from .shelltools import build_shell_tools

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
    "build_cron_tools",
    "build_fileops_tools",
    "build_git_tools",
    "build_inline_tools",
    "build_send_message_tool",
    "build_shell_tools",
    "deny_list",
]

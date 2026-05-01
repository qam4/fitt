"""Agentic tools subsystem.

The public surface is intentionally small at this stage of Phase 4:
type definitions and the registry. Tool implementations, the SSH
backend, and approval/audit pipelines land in later tasks and will
re-export from here once they're stable enough to commit to an API.
"""

from __future__ import annotations

from ._types import (
    ApprovalBucket,
    ApprovalDecision,
    Tool,
    ToolCallable,
    ToolContext,
    ToolResult,
)
from .registry import ToolPolicy, ToolRegistry

__all__ = [
    "ApprovalBucket",
    "ApprovalDecision",
    "Tool",
    "ToolCallable",
    "ToolContext",
    "ToolPolicy",
    "ToolRegistry",
    "ToolResult",
]

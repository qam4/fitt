"""Gateway-specific exceptions.

Centralising these keeps the import graph simple and lets tests pattern-
match on exact types rather than string-matching error messages.
"""

from __future__ import annotations


class GatewayError(Exception):
    """Base class for all gateway errors."""


class ConfigError(GatewayError):
    """Raised when config.yaml or secrets.yaml fails to load or validate."""


class SecretsPermissionError(ConfigError):
    """Raised when the secrets file has unsafe permissions on disk."""


class UnknownAlias(GatewayError):
    """Raised when a request names an alias that isn't configured."""

    def __init__(self, alias: str, available: list[str]) -> None:
        self.alias = alias
        self.available = available
        super().__init__(f"Unknown alias {alias!r}. Available: {', '.join(available) or '(none)'}")


class NoBackendAvailable(GatewayError):
    """Raised when every configured backend for an alias is unreachable."""

    def __init__(self, alias: str, attempted: list[str]) -> None:
        self.alias = alias
        self.attempted = attempted
        super().__init__(
            f"No reachable backend for alias {alias!r}. "
            f"Attempted: {', '.join(attempted) or '(none)'}"
        )


class ModelIdNotAlias(GatewayError):
    """Raised when a request uses a concrete model id instead of an alias."""

    def __init__(self, model: str, available_aliases: list[str]) -> None:
        self.model = model
        self.available_aliases = available_aliases
        super().__init__(
            f"{model!r} looks like a concrete model id, not an alias. "
            f"Use one of: {', '.join(available_aliases)}"
        )


class UnknownSession(GatewayError):
    """Raised when a request names a session id that isn't configured."""

    def __init__(self, session_id: str, available: list[str]) -> None:
        self.session_id = session_id
        self.available = available
        super().__init__(
            f"Unknown session {session_id!r}. Available: {', '.join(available) or '(none)'}"
        )


class MemoryDisabled(GatewayError):
    """Raised when a memory operation is attempted while memory is disabled."""


class UnknownTool(GatewayError):
    """Raised when a request or dispatcher references a tool not in the registry."""

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = available
        super().__init__(f"Unknown tool {name!r}. Available: {', '.join(available) or '(none)'}")


class DuplicateTool(GatewayError):
    """Raised when a caller tries to register a tool whose name is already taken.

    Typically a bug (two inline tools accidentally share a name) or
    a sign of a failed MCP deregister. The registry refuses the
    second registration rather than silently clobbering the first.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"A tool named {name!r} is already registered")

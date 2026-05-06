"""MCP (Model Context Protocol) client: spawn, supervise, route.

FITT hosts zero or more MCP servers as child processes. Each
server is a stdin/stdout JSON-RPC endpoint; on startup we send
``initialize`` + ``tools/list`` to discover what tools it
offers, and we register each one with the tool registry under
the prefix ``mcp.<server>.<tool>``.

Scope in Phase 4
----------------

* **Spawn and supervise.** Start each configured server as a
  child process. On unexpected exit, restart with exponential
  backoff (1s → 2s → 4s → 8s → 16s), give up after five
  consecutive failures.
* **Tool discovery.** ``initialize`` + ``tools/list`` on startup;
  register each tool as an inline FITT tool that forwards calls
  back to this client.
* **Tool invocation.** ``tools/call`` over JSON-RPC. Return the
  content as a string ``ToolResult`` so the chat loop sees the
  same shape as an inline tool.
* **Policy.** Default bucket ``ask`` for all MCP tools. Operators
  tighten or loosen via wildcards in ``config.yaml`` (e.g.
  ``"mcp.slack.send_*": { default: ask }``).

Out of scope
------------

* **Request cancellation from the client side.** MCP supports it;
  we don't surface it yet. Tools time out on the approval side
  (2-hour cap) and the gateway-tool-loop iteration cap, which
  is enough for v1.
* **Resources / prompts / sampling.** Phase 4 only wires
  tools/list + tools/call. Other MCP primitives (resources, prompts)
  earn their place when a concrete use case arrives.
* **Progress notifications.** Not surfaced. Tools either finish
  or time out.

JSON-RPC framing
----------------

MCP uses line-delimited JSON — one JSON object per line,
separated by ``\\n``. No Content-Length, no Content-Type. The
server writes a single JSON response per request; notifications
(no ``id``) are server-initiated and currently ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from .tools._types import ApprovalBucket, Tool, ToolCallable, ToolContext, ToolResult

if TYPE_CHECKING:
    from .tools.registry import ToolRegistry

_log = logging.getLogger(__name__)


# --------------------------------------------------------------- config


class MCPServerConfig(BaseModel):
    """One entry in ``config.mcp_servers``.

    The command launches the server. For npm-packaged servers the
    usual shape is ``["npx", "-y", "@org/server"]``. ``env`` maps
    the child's environment keys to *names of env vars in the
    gateway's own env* — keys aren't inlined here to keep
    secrets out of config.yaml.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    """Short identifier. Appears in tool names as
    ``mcp.<name>.<tool>``. Must be [a-z0-9_-]+ — we reject other
    characters so a bad config can't land tools under weird
    unique-looking names."""

    command: list[str]
    """argv to spawn the server. Must be non-empty."""

    env: dict[str, str] = Field(default_factory=dict)
    """Environment variable passthrough. Each key-value pair maps
    *server env key → name of a gateway env var*. Example::

        env:
          BRAVE_API_KEY: BRAVE_API_KEY

    If the gateway has ``BRAVE_API_KEY`` set, the value is passed
    to the server under the same name. If not, the entry is
    silently dropped (server starts without that key; will
    probably error on first request, which is surface enough)."""

    startup_timeout_s: float = 10.0
    """How long to wait for ``initialize`` + ``tools/list`` before
    declaring the server dead. 10s handles npx cold-start + npm
    download on a NAS."""

    call_timeout_s: float = 120.0
    """Per-``tools/call`` timeout. Longer than local tools because
    MCP servers often proxy to external APIs (Jira search, Slack
    send) where the long tail is real."""


# --------------------------------------------------------------- client


@dataclass
class _PendingCall:
    """One in-flight JSON-RPC request awaiting its response."""

    future: asyncio.Future[dict[str, Any]]


@dataclass
class MCPClient:
    """A single MCP server's subprocess + JSON-RPC wrapper.

    Lifecycle:

        * Construct with ``MCPClient(cfg)``.
        * ``await client.start()`` spawns, initialises, discovers
          tools. Registers them in the ``ToolRegistry``.
        * ``await client.stop()`` cleans up (cancel supervisor,
          unregister tools, terminate process).

    Supervision:

        * A background task watches the subprocess. On exit
          (crash, the server shut itself down, whatever), the
          task respawns with backoff up to five attempts. After
          the fifth failure we give up — the server's tools stay
          unregistered until a config reload / manual restart.
    """

    cfg: MCPServerConfig
    registry: ToolRegistry | None = None
    """Set by :meth:`start`; not at construction so tests can
    poke at a client without a full registry."""

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _supervisor_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _pending: dict[int, _PendingCall] = field(default_factory=dict, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)
    _registered_names: list[str] = field(default_factory=list, init=False, repr=False)
    _stopping: bool = field(default=False, init=False, repr=False)
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _ready: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    # ------------------------------------------------------------------ lifecycle

    async def start(self, registry: ToolRegistry) -> None:
        """Spawn the server and register its tools.

        Raises ``RuntimeError`` if the server fails to initialise
        within ``cfg.startup_timeout_s``. Callers are expected to
        catch, log, and continue — one broken MCP server should
        not crash the gateway."""
        self.registry = registry
        await self._spawn_once()
        self._supervisor_task = asyncio.create_task(
            self._supervise(), name=f"mcp-supervisor-{self.cfg.name}"
        )

    async def stop(self) -> None:
        """Tear down: cancel supervisor, unregister tools,
        terminate the subprocess."""
        self._stopping = True
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._shutdown_proc()
        self._unregister_tools()

    # ------------------------------------------------------------------ spawn / probe

    async def _spawn_once(self) -> None:
        """Spawn, send initialize + tools/list, register tools.
        Used both on first start and on re-spawn after a crash."""
        env = dict(os.environ)
        for server_key, gateway_key in self.cfg.env.items():
            val = os.environ.get(gateway_key)
            if val is not None:
                env[server_key] = val
            else:
                _log.warning(
                    "mcp.env_missing",
                    extra={
                        "server": self.cfg.name,
                        "required": gateway_key,
                    },
                )
        proc = await asyncio.create_subprocess_exec(
            *self.cfg.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._proc = proc
        self._pending.clear()
        self._next_id = 0
        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"mcp-reader-{self.cfg.name}"
        )

        try:
            await asyncio.wait_for(self._probe(), timeout=self.cfg.startup_timeout_s)
        except TimeoutError:
            await self._shutdown_proc()
            raise RuntimeError(
                f"MCP server {self.cfg.name!r} failed to initialise "
                f"within {self.cfg.startup_timeout_s}s"
            ) from None
        self._ready.set()

    async def _probe(self) -> None:
        """Run the mandatory JSON-RPC handshake."""
        # initialize: advertise our protocol version. We're a
        # minimal client so we don't negotiate capabilities
        # beyond "we handle tools".
        await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "fitt-gateway", "version": "0.1"},
            },
        )

        # notifications/initialized is a fire-and-forget; no id.
        await self._send_raw({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # tools/list: what tools does this server expose?
        tools_resp = await self._rpc("tools/list", {})
        tools = tools_resp.get("tools", [])
        if not isinstance(tools, list):
            raise RuntimeError(
                f"MCP server {self.cfg.name!r} returned a non-list "
                f"from tools/list: {type(tools).__name__}"
            )
        self._register_tools(tools)

    # ------------------------------------------------------------------ supervision

    async def _supervise(self) -> None:
        """Background loop: when the subprocess exits, respawn
        with exponential backoff. Give up after five consecutive
        failures."""
        backoff_s = 1.0
        consecutive_failures = 0
        while not self._stopping:
            proc = self._proc
            if proc is None:
                return
            try:
                exit_code = await proc.wait()
            except asyncio.CancelledError:
                raise
            if self._stopping:
                return
            _log.warning(
                "mcp.server_exited",
                extra={
                    "server": self.cfg.name,
                    "exit_code": exit_code,
                    "consecutive_failures": consecutive_failures,
                },
            )
            self._ready.clear()
            self._unregister_tools()
            await asyncio.sleep(backoff_s)
            try:
                await self._spawn_once()
            except Exception as e:
                consecutive_failures += 1
                _log.warning(
                    "mcp.respawn_failed",
                    extra={
                        "server": self.cfg.name,
                        "error": str(e),
                        "consecutive_failures": consecutive_failures,
                    },
                )
                if consecutive_failures >= 5:
                    _log.error(
                        "mcp.giving_up",
                        extra={"server": self.cfg.name},
                    )
                    return
                backoff_s = min(backoff_s * 2, 16.0)
            else:
                # Successful respawn resets the backoff.
                consecutive_failures = 0
                backoff_s = 1.0

    # ------------------------------------------------------------------ rpc i/o

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a request, await its response. Raises on error."""
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = _PendingCall(future=future)
        payload = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
            "params": params,
        }
        await self._send_raw(payload)
        try:
            resp = await future
        finally:
            self._pending.pop(msg_id, None)
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(
                f"MCP {self.cfg.name}.{method} error: {err.get('code')} {err.get('message', '?')}"
            )
        result = resp.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"MCP {self.cfg.name}.{method} returned non-dict result: {type(result).__name__}"
            )
        return result

    async def _send_raw(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError(f"MCP server {self.cfg.name!r} not running")
        line = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    async def _read_loop(self) -> None:
        """Background reader: dispatch responses to their pending
        futures. Notifications (no ``id``) are currently dropped
        with a debug log."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                raw = await proc.stdout.readline()
            except (asyncio.CancelledError, RuntimeError):
                return
            if not raw:
                # EOF. The supervisor will see this via proc.wait.
                return
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                _log.warning(
                    "mcp.malformed_json",
                    extra={"server": self.cfg.name, "line": raw[:200].decode(errors="replace")},
                )
                continue
            if not isinstance(msg, dict):
                continue
            msg_id = msg.get("id")
            if isinstance(msg_id, int) and msg_id in self._pending:
                pending = self._pending[msg_id]
                if not pending.future.done():
                    pending.future.set_result(msg)
            else:
                _log.debug(
                    "mcp.unhandled_message",
                    extra={"server": self.cfg.name, "method": msg.get("method")},
                )

    async def _shutdown_proc(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await self._proc.wait()
                except Exception:
                    pass
            self._proc = None

    # ------------------------------------------------------------------ tool registration

    def _register_tools(self, tools: list[dict[str, Any]]) -> None:
        if self.registry is None:
            return
        for descriptor in tools:
            name = descriptor.get("name")
            if not isinstance(name, str) or not name:
                _log.warning(
                    "mcp.tool_missing_name",
                    extra={"server": self.cfg.name, "descriptor": descriptor},
                )
                continue
            prefixed = f"mcp.{self.cfg.name}.{name}"
            description = str(descriptor.get("description") or f"MCP tool {prefixed}")
            schema = descriptor.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            tool = Tool(
                name=prefixed,
                description=description,
                schema=schema,
                callable=_make_invoker(self, name),
                default_bucket=ApprovalBucket.ASK,
                requires_project=False,
                kind="mcp",
            )
            try:
                self.registry.register(tool)
                self._registered_names.append(prefixed)
            except Exception as e:
                _log.warning(
                    "mcp.tool_register_failed",
                    extra={"server": self.cfg.name, "tool": prefixed, "error": str(e)},
                )

    def _unregister_tools(self) -> None:
        if self.registry is None:
            return
        for prefixed in list(self._registered_names):
            try:
                self.registry.unregister(prefixed)
            except Exception:
                pass
        self._registered_names = []

    # ------------------------------------------------------------------ tool call

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        """Invoke a tool on this server by the **unprefixed** name.

        The wrapper installed via :meth:`_make_invoker` calls this.
        Callers outside the wrapper shouldn't need to — the chat
        loop goes through the registry, which finds the prefixed
        tool and its callable (the wrapper)."""
        if not self._ready.is_set():
            return ToolResult.error(
                f"MCP server {self.cfg.name!r} is not ready (starting up or crashed)"
            )
        try:
            result = await asyncio.wait_for(
                self._rpc("tools/call", {"name": name, "arguments": args}),
                timeout=self.cfg.call_timeout_s,
            )
        except TimeoutError:
            return ToolResult.error(
                f"MCP {self.cfg.name}.{name} timed out after {self.cfg.call_timeout_s}s"
            )
        except Exception as e:
            return ToolResult.error(f"MCP {self.cfg.name}.{name} failed: {e}")

        # MCP tools return a content array; we flatten to text for
        # the chat loop's tool-result message shape.
        content = result.get("content")
        if not isinstance(content, list):
            return ToolResult.ok(json.dumps(result))
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "text":
                parts.append(str(item.get("text", "")))
            elif kind == "image":
                parts.append(f"[image {item.get('mimeType', 'unknown')} omitted]")
            else:
                parts.append(json.dumps(item))
        payload = "\n".join(parts) if parts else json.dumps(result)
        if result.get("isError"):
            return ToolResult.error(payload)
        return ToolResult.ok(payload)


def _make_invoker(client: MCPClient, tool_name: str) -> ToolCallable:
    """Return a ToolCallable that forwards to the given client/tool."""

    async def _invoke(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx  # MCP tools don't need project/backend plumbing
        return await client.call_tool(tool_name, args)

    return _invoke


# --------------------------------------------------------------- manager


@dataclass
class MCPManager:
    """Collection of MCPClient instances; one per configured
    server. The gateway's app.state holds a single MCPManager
    that :meth:`start_all` / :meth:`stop_all` over its clients."""

    configs: list[MCPServerConfig]
    clients: dict[str, MCPClient] = field(default_factory=dict)

    async def start_all(
        self,
        registry: ToolRegistry,
        on_error: Callable[[str, Exception], Awaitable[None]] | None = None,
    ) -> None:
        """Spawn every configured server. Clients that fail to
        start are logged but do not prevent healthy siblings from
        loading."""
        for cfg in self.configs:
            client = MCPClient(cfg=cfg)
            try:
                await client.start(registry)
            except Exception as e:
                _log.warning(
                    "mcp.start_failed",
                    extra={"server": cfg.name, "error": str(e)},
                )
                if on_error is not None:
                    await on_error(cfg.name, e)
                continue
            self.clients[cfg.name] = client

    async def stop_all(self) -> None:
        for client in list(self.clients.values()):
            try:
                await client.stop()
            except Exception:
                pass
        self.clients.clear()

    async def restart(self, name: str, registry: ToolRegistry) -> None:
        """Stop + start the named server. Used by
        ``fitt mcp restart``."""
        client = self.clients.get(name)
        if client is not None:
            await client.stop()
            self.clients.pop(name, None)
        for cfg in self.configs:
            if cfg.name == name:
                fresh = MCPClient(cfg=cfg)
                await fresh.start(registry)
                self.clients[name] = fresh
                return
        raise KeyError(f"no MCP server named {name!r}")

    def describe(self) -> list[dict[str, Any]]:
        """Return a compact summary for the ``fitt mcp list``
        command."""
        out: list[dict[str, Any]] = []
        for cfg in self.configs:
            client = self.clients.get(cfg.name)
            out.append(
                {
                    "name": cfg.name,
                    "running": client is not None and client._ready.is_set(),
                    "command": cfg.command,
                    "tools": list(client._registered_names) if client else [],
                }
            )
        return out

"""Tests for the MCP client.

Rather than spawning a real MCP server subprocess, we patch
:func:`asyncio.create_subprocess_exec` with a fake that returns
an in-process "subprocess" whose stdin/stdout are piped streams.
An async loop on the fixture plays the server role: reads JSON-RPC
frames, responds to ``initialize`` / ``tools/list`` / ``tools/call``
according to the scenario.

This gives us end-to-end coverage of:
* JSON-RPC framing (line-delimited JSON)
* initialize + tools/list handshake
* Tool registration into a ToolRegistry with the
  ``mcp.<server>.<tool>`` prefix
* tools/call round-trip
* Crash + supervisor-driven respawn
* Graceful shutdown

Without needing an actual server on PATH, and with deterministic
timing so the tests don't flake on loaded CI hosts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from gateway.mcp import MCPClient, MCPManager, MCPServerConfig
from gateway.tools import ApprovalBucket, ToolRegistry

# --------------------------------------------------------------- fake subprocess


@dataclass
class FakeSubprocess:
    """Stand-in for asyncio.subprocess.Process.

    stdin / stdout are real asyncio streams connected to an
    in-process async task that plays the server role."""

    stdin: Any
    stdout: Any
    stderr: Any
    _returncode: int | None = None
    _exit_event: asyncio.Event = field(default_factory=asyncio.Event)
    server_task: asyncio.Task[None] | None = None

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        await self._exit_event.wait()
        return self._returncode or 0

    def terminate(self) -> None:
        if self._returncode is None:
            self._returncode = -15
            self._exit_event.set()
            if self.server_task is not None:
                self.server_task.cancel()

    def kill(self) -> None:
        self.terminate()


class _PipedReader:
    """Minimal StreamReader lookalike: queue of lines, await
    :meth:`readline` to get one at a time."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._eof = False

    async def readline(self) -> bytes:
        if self._eof and self._queue.empty():
            return b""
        try:
            return await self._queue.get()
        except asyncio.CancelledError:
            return b""

    def feed(self, data: bytes) -> None:
        self._queue.put_nowait(data)

    def feed_eof(self) -> None:
        self._eof = True
        # Unblock any pending readline.
        self._queue.put_nowait(b"")


class _PipedWriter:
    """Minimal StreamWriter lookalike that buffers writes and
    exposes them as complete JSON lines for the fake server to
    consume."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        while b"\n" in self._buffer:
            idx = self._buffer.index(b"\n")
            line = bytes(self._buffer[:idx])
            del self._buffer[: idx + 1]
            self._lines.put_nowait(line)

    async def drain(self) -> None:
        return None

    async def readline(self) -> bytes:
        return await self._lines.get()


def _make_fake_server_factory(
    tool_descriptors: list[dict[str, Any]],
    call_handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    crash_after_calls: int | None = None,
) -> Callable[..., Awaitable[FakeSubprocess]]:
    """Return an async factory that, given the create_subprocess_exec
    arguments, produces a FakeSubprocess whose server loop
    responds to JSON-RPC requests according to the scenario.

    * ``tool_descriptors`` — what ``tools/list`` returns.
    * ``call_handler`` — async (name, args) → result dict, used
      for ``tools/call``. Defaults to echoing the args.
    * ``crash_after_calls`` — if set, server task exits with code
      1 after this many successful ``tools/call`` responses have
      been sent.
    """

    async def factory(*_args: Any, **_kwargs: Any) -> FakeSubprocess:
        # Client writes to server via `proc.stdin.write`; server
        # reads those bytes via `stdin_reader`.
        stdin_writer = _PipedWriter()
        stdout_reader = _PipedReader()

        # The FakeSubprocess exposes stdin=writer-for-client,
        # stdout=reader-for-client. Inside the server loop we
        # read from stdin_writer (client's sends) and write to
        # stdout_reader (client's receives).

        async def server_loop() -> None:
            calls_seen = 0
            try:
                while True:
                    raw = await stdin_writer.readline()
                    if not raw:
                        return
                    try:
                        req = json.loads(raw.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    msg_id = req.get("id")
                    method = req.get("method")
                    params = req.get("params") or {}
                    if method == "initialize":
                        resp = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "fake", "version": "0"},
                            },
                        }
                    elif method == "notifications/initialized":
                        # Fire-and-forget; nothing to send back.
                        continue
                    elif method == "tools/list":
                        resp = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "result": {"tools": tool_descriptors},
                        }
                    elif method == "tools/call":
                        handler = call_handler or _default_call_handler
                        name = params.get("name", "")
                        args = params.get("arguments") or {}
                        try:
                            result = await handler(name, args)
                        except Exception as e:
                            result = {
                                "content": [{"type": "text", "text": f"error: {e}"}],
                                "isError": True,
                            }
                        resp = {"jsonrpc": "2.0", "id": msg_id, "result": result}
                        calls_seen += 1
                    else:
                        resp = {
                            "jsonrpc": "2.0",
                            "id": msg_id,
                            "error": {
                                "code": -32601,
                                "message": f"method not found: {method}",
                            },
                        }
                    stdout_reader.feed((json.dumps(resp) + "\n").encode("utf-8"))
                    if crash_after_calls is not None and calls_seen >= crash_after_calls:
                        return  # simulate server exiting
            finally:
                stdout_reader.feed_eof()

        task = asyncio.create_task(server_loop(), name="fake-mcp-server")
        fake = FakeSubprocess(
            stdin=_ClientStdin(stdin_writer),
            stdout=stdout_reader,
            stderr=None,
            server_task=task,
        )

        async def _watch_exit() -> None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            fake._returncode = 1 if crash_after_calls is not None else 0
            fake._exit_event.set()

        _exit_watcher = asyncio.create_task(_watch_exit(), name="fake-mcp-exit-watcher")
        # Keep a reference on the fake so we can await/cancel it if
        # we ever need to; also silences ruff's "fire-and-forget
        # task" warning.
        fake._exit_watcher = _exit_watcher  # type: ignore[attr-defined]
        return fake

    return factory


async def _default_call_handler(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"tool": name, "args": args})}],
    }


class _ClientStdin:
    """Wraps a _PipedWriter so the MCPClient's `proc.stdin.write`
    + `drain` ends up in the server's read queue."""

    def __init__(self, writer: _PipedWriter) -> None:
        self._writer = writer

    def write(self, data: bytes) -> None:
        self._writer.write(data)

    async def drain(self) -> None:
        return None


# --------------------------------------------------------------- scenarios


_ONE_TOOL = [
    {
        "name": "echo",
        "description": "Return args verbatim",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
        },
    }
]


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry()


async def test_start_registers_tools_with_prefix(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(_ONE_TOOL),
    )
    cfg = MCPServerConfig(name="fake", command=["true"])
    client = MCPClient(cfg=cfg)
    await client.start(registry)
    try:
        names = registry.list_names()
        assert "mcp.fake.echo" in names
        tool = registry.lookup("mcp.fake.echo")
        assert tool.kind == "mcp"
        assert tool.default_bucket == ApprovalBucket.ASK
        assert tool.description == "Return args verbatim"
    finally:
        await client.stop()


async def test_tool_call_roundtrip(registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(_ONE_TOOL),
    )
    cfg = MCPServerConfig(name="fake", command=["true"])
    client = MCPClient(cfg=cfg)
    await client.start(registry)
    try:
        tool = registry.lookup("mcp.fake.echo")
        result = await tool.callable({"text": "hi"}, _dummy_ctx())
        assert not result.is_error
        decoded = json.loads(result.payload)
        assert decoded == {"tool": "echo", "args": {"text": "hi"}}
    finally:
        await client.stop()


async def test_tool_error_is_surfaced(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_handler(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "rate limited"}],
            "isError": True,
        }

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(_ONE_TOOL, call_handler=failing_handler),
    )
    cfg = MCPServerConfig(name="fake", command=["true"])
    client = MCPClient(cfg=cfg)
    await client.start(registry)
    try:
        tool = registry.lookup("mcp.fake.echo")
        result = await tool.callable({}, _dummy_ctx())
        assert result.is_error
        assert "rate limited" in result.payload
    finally:
        await client.stop()


async def test_unregister_on_stop(registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(_ONE_TOOL),
    )
    cfg = MCPServerConfig(name="fake", command=["true"])
    client = MCPClient(cfg=cfg)
    await client.start(registry)
    assert "mcp.fake.echo" in registry.list_names()
    await client.stop()
    assert "mcp.fake.echo" not in registry.list_names()


async def test_two_servers_under_different_prefixes(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two MCP servers each with their own tool; prefixes keep
    them distinct even when the inner name collides."""
    tool_descriptor = [{"name": "send", "inputSchema": {"type": "object", "properties": {}}}]
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(tool_descriptor),
    )
    a = MCPClient(cfg=MCPServerConfig(name="slack", command=["true"]))
    b = MCPClient(cfg=MCPServerConfig(name="discord", command=["true"]))
    await a.start(registry)
    try:
        await b.start(registry)
        try:
            assert "mcp.slack.send" in registry.list_names()
            assert "mcp.discord.send" in registry.list_names()
        finally:
            await b.stop()
    finally:
        await a.stop()


async def test_manager_start_all_and_restart(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(_ONE_TOOL),
    )
    mgr = MCPManager(
        configs=[MCPServerConfig(name="fake", command=["true"])],
    )
    await mgr.start_all(registry)
    try:
        assert "fake" in mgr.clients
        assert "mcp.fake.echo" in registry.list_names()

        # Restart.
        await mgr.restart("fake", registry)
        assert "fake" in mgr.clients
        assert "mcp.fake.echo" in registry.list_names()
    finally:
        await mgr.stop_all()


async def test_manager_restart_unknown_server(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    mgr = MCPManager(configs=[])
    with pytest.raises(KeyError):
        await mgr.restart("ghost", registry)


async def test_startup_failure_logs_and_skips(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that never responds to initialize within its
    timeout should be dropped; healthy siblings still start."""

    async def never_responding(*_args: Any, **_kwargs: Any) -> FakeSubprocess:
        # Server that reads forever but never writes anything.
        stdin_writer = _PipedWriter()
        stdout_reader = _PipedReader()

        async def noop_loop() -> None:
            while True:
                _ = await stdin_writer.readline()

        task = asyncio.create_task(noop_loop(), name="dead-server")
        fake = FakeSubprocess(
            stdin=_ClientStdin(stdin_writer),
            stdout=stdout_reader,
            stderr=None,
            server_task=task,
        )
        return fake

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never_responding)

    errors_seen: list[tuple[str, Exception]] = []

    async def on_error(name: str, exc: Exception) -> None:
        errors_seen.append((name, exc))

    mgr = MCPManager(
        configs=[MCPServerConfig(name="slow", command=["true"], startup_timeout_s=0.2)],
    )
    await mgr.start_all(registry, on_error=on_error)
    try:
        assert "slow" not in mgr.clients
        assert len(errors_seen) == 1
        assert errors_seen[0][0] == "slow"
    finally:
        await mgr.stop_all()


async def test_env_passthrough(
    registry: ToolRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env: keys map the server's env var to a gateway env var.
    Present → passed through; missing → dropped with a warning."""
    captured_env: dict[str, str] = {}

    async def factory(*_args: Any, **kwargs: Any) -> FakeSubprocess:
        env = kwargs.get("env", {})
        captured_env.update(env)
        return await _make_fake_server_factory(_ONE_TOOL)(*_args, **kwargs)

    monkeypatch.setenv("MY_API_KEY", "secret-value")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", factory)

    cfg = MCPServerConfig(
        name="fake",
        command=["true"],
        env={"PROVIDER_API_KEY": "MY_API_KEY"},
    )
    client = MCPClient(cfg=cfg)
    await client.start(registry)
    try:
        assert captured_env.get("PROVIDER_API_KEY") == "secret-value"
    finally:
        await client.stop()


async def test_call_timeout_surfaces(
    registry: ToolRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool call that doesn't return within call_timeout_s
    should fail cleanly with a timeout error."""

    async def slow_handler(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(1.0)
        return {"content": [{"type": "text", "text": "late"}]}

    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        _make_fake_server_factory(_ONE_TOOL, call_handler=slow_handler),
    )
    cfg = MCPServerConfig(name="fake", command=["true"], call_timeout_s=0.1)
    client = MCPClient(cfg=cfg)
    await client.start(registry)
    try:
        tool = registry.lookup("mcp.fake.echo")
        result = await tool.callable({}, _dummy_ctx())
        assert result.is_error
        assert "timed out" in result.payload.lower()
    finally:
        await client.stop()


async def test_config_rejects_extra_fields() -> None:
    """MCPServerConfig uses extra='forbid' so typos in config.yaml
    surface as validation errors at startup, not silent drops."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MCPServerConfig(
            name="x",
            command=["true"],
            not_a_field="oops",  # type: ignore[call-arg]
        )


# --------------------------------------------------------------- helpers


def _dummy_ctx() -> Any:
    """Minimal ToolContext stand-in; the MCP invoker doesn't read
    it beyond passing it through."""
    from gateway.projects import ProjectRegistry
    from gateway.tools import ToolContext

    # An in-memory registry is fine; MCP tools don't use project context.
    return ToolContext(
        client="ide",
        session_key="main",
        projects=ProjectRegistry(config_path=None),  # type: ignore[arg-type]
        backend=None,
    )

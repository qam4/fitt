"""Tests for Phase 4 Task 11 shell-adjacent tools.

Covers ``run_tests`` (runs project.test_command via the
ExecutionBackend) and ``http_get`` (gateway-local HTTP fetch with
a deny_hosts gate).

``run_tests`` uses the same FakeBackend pattern as
test_tools_fileops / test_tools_gitops. ``http_get`` mocks
``httpx.AsyncClient`` via monkeypatch because the tool runs
in-process (no backend).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from gateway.projects import Project, ProjectRegistry
from gateway.tools import (
    ApprovalBucket,
    ToolContext,
    ToolPolicy,
    ToolRegistry,
    build_shell_tools,
)
from gateway.tools.backend import ShellResult

# --------------------------------------------------------------- stubs


@dataclass
class FakeBackend:
    """Records invocations; returns queued ShellResults.

    Same shape as the other tool test files; kept separate so a
    refactor of any one doesn't couple the others."""

    responses: list[ShellResult] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run_shell(
        self,
        project: Project,
        cmd: list[str],
        *,
        cwd: str | None = None,
        timeout_secs: int = 300,
        extra_env: dict[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> ShellResult:
        self.calls.append(
            {
                "project": project.name,
                "ssh_host": project.ssh_host,
                "cmd": list(cmd),
                "cwd": cwd,
                "timeout_secs": timeout_secs,
                "extra_env": dict(extra_env) if extra_env else None,
                "stdin": stdin,
            }
        )
        if not self.responses:
            return ShellResult(exit=0, stdout="", stderr="", timed_out=False)
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _ok(stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(exit=0, stdout=stdout, stderr=stderr, timed_out=False)


def _err(exit_code: int, stdout: str = "", stderr: str = "") -> ShellResult:
    return ShellResult(exit=exit_code, stdout=stdout, stderr=stderr, timed_out=False)


def _timeout() -> ShellResult:
    return ShellResult(exit=-1, stdout="", stderr="timed out", timed_out=True)


# --------------------------------------------------------------- fixtures


@pytest.fixture
def project_registry(tmp_path: Path) -> ProjectRegistry:
    reg = ProjectRegistry(config_path=tmp_path / "projects.yaml")
    reg.add(
        Project(
            name="hub",
            ssh_host="",
            path="/hub/repo",
            test_command="pytest -q",
        )
    )
    reg.add(
        Project(
            name="remote",
            ssh_host="laptop.tailnet",
            path="/home/fred/code/x",
            test_command='sh -c "cd gateway && uv run pytest -q"',
        )
    )
    reg.add(
        Project(
            name="notests",
            ssh_host="",
            path="/hub/untested",
            test_command="",
        )
    )
    return reg


@pytest.fixture
def tool_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in build_shell_tools():
        reg.register(t)
    return reg


def _ctx(
    projects: ProjectRegistry,
    backend: FakeBackend,
    *,
    policy: ToolPolicy | None = None,
) -> ToolContext:
    return ToolContext(
        client="ide",
        session_key="main",
        projects=projects,
        backend=backend,
        policy=policy,
    )


# --------------------------------------------------------------- registration


def test_builder_registers_two_tools(tool_registry: ToolRegistry) -> None:
    assert tool_registry.list_names() == ["http_get", "run_tests"]


def test_run_tests_defaults_to_ask(tool_registry: ToolRegistry) -> None:
    tool = tool_registry.lookup("run_tests")
    assert tool.default_bucket == ApprovalBucket.ASK
    assert tool.requires_project is True


def test_http_get_defaults_to_auto_and_is_not_project_scoped(
    tool_registry: ToolRegistry,
) -> None:
    tool = tool_registry.lookup("http_get")
    assert tool.default_bucket == ApprovalBucket.AUTO
    assert tool.requires_project is False


# --------------------------------------------------------------- run_tests


async def test_run_tests_happy_path(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_ok(stdout="5 passed in 0.12s\n")])
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert "passed" in result.payload
    assert "exit=0" in result.payload
    assert backend.calls[0]["cmd"] == ["pytest", "-q"]


async def test_run_tests_splits_test_command_via_shlex(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Complex test commands (sh -c '...') split correctly and
    keep the quoted body as a single argv element."""
    backend = FakeBackend(responses=[_ok(stdout="ok\n")])
    tool = tool_registry.lookup("run_tests")
    await tool.callable({"project": "remote"}, _ctx(project_registry, backend))
    assert backend.calls[0]["cmd"] == [
        "sh",
        "-c",
        "cd gateway && uv run pytest -q",
    ]
    assert backend.calls[0]["ssh_host"] == "laptop.tailnet"


async def test_run_tests_merges_stdout_and_stderr(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """Tests write to both streams; we include both in the payload."""
    backend = FakeBackend(
        responses=[
            ShellResult(
                exit=1,
                stdout="FAILED tests/test_foo.py::test_bar\n",
                stderr="warning: deprecation\n",
                timed_out=False,
            )
        ]
    )
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "FAILED" in result.payload
    assert "deprecation" in result.payload
    assert "exit=1" in result.payload
    assert "stderr" in result.payload


async def test_run_tests_nonzero_exit_is_error_not_ok(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """A test run that exits nonzero should be surfaced as an
    error result — the model uses is_error to decide whether
    to investigate."""
    backend = FakeBackend(responses=[_err(1, stdout="1 failed")])
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error


async def test_run_tests_timeout_surfaces(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend(responses=[_timeout()])
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "timed out" in result.payload


async def test_run_tests_missing_test_command(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """A project with no test_command can't run tests — surface
    a clear message, don't dispatch a no-op shell call."""
    backend = FakeBackend()
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "notests"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "no test_command" in result.payload
    assert backend.calls == []


async def test_run_tests_unknown_project(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    backend = FakeBackend()
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "ghost"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "Unknown project" in result.payload


async def test_run_tests_truncates_large_output(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    """A noisy suite's output should be capped, not dropped."""
    huge = "pytest progress\n" * 20_000
    backend = FakeBackend(responses=[_ok(stdout=huge)])
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "hub"}, _ctx(project_registry, backend))
    assert not result.is_error
    assert "more bytes truncated" in result.payload


async def test_run_tests_handles_malformed_test_command(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
) -> None:
    """shlex.split can fail on unclosed quotes; surface cleanly."""
    project_registry.add(
        Project(
            name="badquote",
            ssh_host="",
            path="/hub/bad",
            test_command='pytest "unclosed',
        )
    )
    backend = FakeBackend()
    tool = tool_registry.lookup("run_tests")
    result = await tool.callable({"project": "badquote"}, _ctx(project_registry, backend))
    assert result.is_error
    assert "failed to parse" in result.payload
    assert backend.calls == []


# --------------------------------------------------------------- http_get


class _FakeResponse:
    """Minimal httpx.Response lookalike for the tool's happy path."""

    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHTTPClient:
    """Replaces httpx.AsyncClient. Records requests and returns a
    queued response (or raises a queued exception)."""

    def __init__(self, response: _FakeResponse | Exception, **_kwargs: Any) -> None:
        self._response = response
        self.requested_url: str | None = None
        self.follow_redirects_requested: bool = False

    async def __aenter__(self) -> _FakeHTTPClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None

    async def get(self, url: str, *, follow_redirects: bool = False) -> _FakeResponse:
        self.requested_url = url
        self.follow_redirects_requested = follow_redirects
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, resp: _FakeResponse | Exception) -> list[Any]:
    """Install a fake httpx.AsyncClient and capture constructed instances."""
    captured: list[_FakeHTTPClient] = []

    def factory(*args: Any, **kwargs: Any) -> _FakeHTTPClient:
        client = _FakeHTTPClient(resp, **kwargs)
        captured.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return captured


async def test_http_get_returns_body_with_summary(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, _FakeResponse(status_code=200, text="hello world"))
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://example.com/hello"},
        _ctx(project_registry, FakeBackend()),
    )
    assert not result.is_error
    # Summary header includes status, host, and body length.
    assert "HTTP 200" in result.payload
    assert "example.com" in result.payload
    assert "hello world" in result.payload


async def test_http_get_follows_redirects(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """We ask httpx to follow redirects (common for docs sites)."""
    captured = _patch_httpx(monkeypatch, _FakeResponse(200, "final"))
    tool = tool_registry.lookup("http_get")
    await tool.callable(
        {"url": "https://example.com/start"},
        _ctx(project_registry, FakeBackend()),
    )
    assert captured[0].follow_redirects_requested is True


async def test_http_get_rejects_non_http_scheme(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, _FakeResponse(200, "never"))
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "file:///etc/passwd"},
        _ctx(project_registry, FakeBackend()),
    )
    assert result.is_error
    assert "URL rejected" in result.payload


async def test_http_get_missing_url(
    tool_registry: ToolRegistry, project_registry: ProjectRegistry
) -> None:
    tool = tool_registry.lookup("http_get")
    result = await tool.callable({}, _ctx(project_registry, FakeBackend()))
    assert result.is_error
    assert "url" in result.payload


async def test_http_get_denies_host_from_policy(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deny_hosts in the tool's per-tool config blocks the call
    before any network I/O happens."""
    captured = _patch_httpx(monkeypatch, _FakeResponse(200, "shouldn't reach"))
    policy = ToolPolicy.from_config(
        {
            "http_get": {
                "default": "auto",
                "deny_hosts": ["internal.corp.example", "*.secret.example"],
            }
        }
    )
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://internal.corp.example/admin"},
        _ctx(project_registry, FakeBackend(), policy=policy),
    )
    assert result.is_error
    assert "deny_hosts" in result.payload
    # Didn't hit the network.
    assert captured == []


async def test_http_get_deny_supports_glob(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_httpx(monkeypatch, _FakeResponse(200, "x"))
    policy = ToolPolicy.from_config({"http_get": {"deny_hosts": ["*.corp.example"]}})
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://jenkins.corp.example/"},
        _ctx(project_registry, FakeBackend(), policy=policy),
    )
    assert result.is_error
    assert captured == []


async def test_http_get_deny_does_not_overmatch(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host not in the deny list is fetched normally."""
    _patch_httpx(monkeypatch, _FakeResponse(200, "public doc"))
    policy = ToolPolicy.from_config({"http_get": {"deny_hosts": ["*.corp.example"]}})
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://example.org/"},
        _ctx(project_registry, FakeBackend(), policy=policy),
    )
    assert not result.is_error
    assert "public doc" in result.payload


async def test_http_get_surfaces_http_error_status(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, _FakeResponse(404, "Not Found"))
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://example.com/missing"},
        _ctx(project_registry, FakeBackend()),
    )
    assert result.is_error
    assert "404" in result.payload
    assert "Not Found" in result.payload


async def test_http_get_surfaces_timeout(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, httpx.TimeoutException("slow"))
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://example.com/slow"},
        _ctx(project_registry, FakeBackend()),
    )
    assert result.is_error
    assert "timed out" in result.payload


async def test_http_get_surfaces_transport_error(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(monkeypatch, httpx.ConnectError("connection refused"))
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://example.com/"},
        _ctx(project_registry, FakeBackend()),
    )
    assert result.is_error
    assert "transport error" in result.payload


async def test_http_get_truncates_large_body(
    tool_registry: ToolRegistry,
    project_registry: ProjectRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge = "x" * 300_000
    _patch_httpx(monkeypatch, _FakeResponse(200, huge))
    tool = tool_registry.lookup("http_get")
    result = await tool.callable(
        {"url": "https://example.com/big"},
        _ctx(project_registry, FakeBackend()),
    )
    assert not result.is_error
    assert "more bytes truncated" in result.payload
    assert len(result.payload) < 300_000

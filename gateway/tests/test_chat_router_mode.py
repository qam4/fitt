"""Tests for router-mode pass-through for coding-CLI clients.

What router mode means
----------------------

Clients that own their own agent loop (Aider, Claude Code,
Cursor agent mode, Codex, Kiro CLI, ...) send
``X-FITT-Client: coding-cli``. For those clients, FITT behaves
as a thin alias-routing proxy:

* No capability block in the system prompt.
* No FITT tools merged into the ``tools`` array.
* No memory / lessons / identity injection.
* No FITT tool-loop dispatch (the client drives its own).
* No approval middleware (the client owns that UX).

What FITT still does for router-mode clients:

* Alias resolution (``fitt-smart`` → the right backend).
* Backend dispatch via LiteLLM.
* Cost tracking (accounting works the same).
* Audit log (the model call gets logged; tool calls don't
  because none happen inside the gateway loop).

Regression guard for the 2026-05-11 Aider collision where
pointing Aider at FITT caused FITT's own tool list to land
inside Aider's agent context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.projects import Project

from ._fixtures import PERSONAL_TOKEN, build_test_config
from ._llm_stubs import make_response, make_tool_call


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


def _coding_cli_headers() -> dict[str, str]:
    return {
        **_auth(),
        "X-FITT-Client": "coding-cli",
    }


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    app = create_app(cfg)

    # Register a hub-local project so FITT tools have somewhere
    # to resolve to. (They shouldn't be reached in router mode
    # tests, but other assertions run against the same client
    # fixture.)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Hello\n", encoding="utf-8")
    app.state.project_registry.add(
        Project(name="hub", ssh_host="", path=str(repo), test_command="pytest -q")
    )

    # Seed an identity file so the "is memory injected?"
    # assertions have something meaningful to check for.
    identity_dir = cfg.memory.identity_dir
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "user.md").write_text(
        "# User identity\nFred prefers direct answers.\n", encoding="utf-8"
    )
    return TestClient(app)


# --------------------------------------------------------------- injection


def test_coding_cli_gets_no_capability_block(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The capability block names FITT's tools. In router mode,
    Aider's own agent should not see FITT's tool list — that's
    the Mode 1 / Mode 2 collision we're closing.

    Assert by snooping on the dispatch body: no system message
    (or no FITT capability-block content in one) should reach
    the upstream model."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hello"}],
        },
        headers=_coding_cli_headers(),
    )
    assert r.status_code == 200, r.text

    msgs = captured[0]["messages"]
    # No system message at all in router mode: memory injection
    # is skipped entirely and a bare user turn has no system
    # prefix.
    assert all(m["role"] != "system" for m in msgs), (
        f"expected no system message in router mode, got: {msgs}"
    )


def test_coding_cli_tools_array_passes_through_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Aider sends its own ``tools`` array (its file-edit,
    diff, shell tools), that array must reach the model byte-
    for-byte. No FITT tools merged on top — merging would
    confuse Aider's agent."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_response(content="nothing to do")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    aider_tool = {
        "type": "function",
        "function": {
            "name": "aider_apply_edits",
            "description": "Aider's own edit tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "fix the bug"}],
            "tools": [aider_tool],
        },
        headers=_coding_cli_headers(),
    )
    assert r.status_code == 200, r.text

    tools_sent = captured[0].get("tools") or []
    # Only Aider's own tool reaches the model — no FITT tools
    # appended.
    names = [t["function"]["name"] for t in tools_sent]
    assert names == ["aider_apply_edits"], f"expected only Aider's own tool; got: {names}"


def test_coding_cli_skips_memory_injection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity files under $FITT_HOME/identity shouldn't land
    in a coding-CLI request. Aider has its own conventions
    file; layering FITT's identity on top noise-injects the
    client's agent."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_coding_cli_headers(),
    )
    assert r.status_code == 200, r.text

    # Identity file contains "Fred prefers direct answers."
    # Verify that string is nowhere in the dispatched body.
    body_str = str(captured[0])
    assert "Fred prefers direct answers" not in body_str


def test_coding_cli_does_not_run_fitt_tool_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the model returns tool_calls naming a FITT tool,
    router mode passes the response through without executing
    any tools. The coding-CLI client will take the tool_calls
    and either execute them in its own runtime or reject them.

    Assert by checking we only see ONE upstream dispatch: a
    second dispatch would mean FITT's tool loop fired, pulled
    the tool_calls out, executed them, and re-dispatched."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        # Imagine Aider or Claude Code saw its own tool in the
        # array and asked for it; router mode passes the reply
        # through regardless.
        return make_response(tool_calls=[make_tool_call("c1", "aider_apply_edits", {})])

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "fix the bug"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "aider_apply_edits",
                        "description": "",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
            ],
            "tool_choice": "auto",
        },
        headers=_coding_cli_headers(),
    )
    assert r.status_code == 200, r.text
    # Exactly one dispatch: no FITT tool loop.
    assert len(captured) == 1
    # Response carries the tool_calls through untouched — Aider
    # reads them and decides what to do.
    body = r.json()
    msg = body["choices"][0]["message"]
    assert msg.get("tool_calls"), "router mode should pass tool_calls through"


# --------------------------------------------------------------- alias resolution


def test_coding_cli_still_resolves_aliases(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Router mode skips the agent layering but still resolves
    aliases through the config — that's the WHOLE POINT of
    pointing Aider at FITT instead of at Ollama directly.
    ``model: fitt-smart`` must land the request on the model
    the alias points at, not on a literal ``fitt-smart``.

    Assertion: the dispatched model string is NOT the alias."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_coding_cli_headers(),
    )
    assert r.status_code == 200, r.text
    # The alias resolved to openrouter-sonnet (from the test
    # fixture), so LiteLLM sees the prefixed name, not "fitt-smart".
    model_sent = captured[0]["model"]
    assert model_sent != "fitt-smart"
    assert "fitt-smart" not in model_sent


def test_coding_cli_rejects_concrete_model_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aliases-only discipline applies even in router mode —
    otherwise the "swap a model by editing config.yaml" promise
    breaks for the coding-CLI use case. If someone tries to
    send a concrete model id, we reject with the usual 400."""

    async def fake(**kwargs: Any) -> Any:
        raise AssertionError("dispatch should not run for rejected requests")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "qwen2.5-coder:14b",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_coding_cli_headers(),
    )
    # Same 400 as the non-router path — the error handling is
    # unchanged.
    assert r.status_code == 400, r.text


# --------------------------------------------------------------- non-router


def test_telegram_client_still_gets_capability_block(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: other clients (telegram, webui, cli, ide)
    are NOT affected by the router-mode path. Their requests
    still get identity / capability / tool injection. This
    test pins the non-router behaviour so a future refactor
    that subtly broadens the router-mode gate gets caught."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={**_auth(), "X-FITT-Client": "telegram"},
    )
    assert r.status_code == 200, r.text

    msgs = captured[0]["messages"]
    # Telegram gets a system message with FITT identity content.
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert system_msgs, "telegram client should get a system prefix"
    joined = "\n".join(str(m["content"]) for m in system_msgs)
    assert "Fred prefers direct answers" in joined


def test_unknown_client_defaults_to_agent_mode_not_router(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bearer token with no ``client:`` tag and no
    ``X-FITT-Client`` header defaults to ``webui`` (per auth.py
    resolution). That means the default for unknown clients is
    AGENT mode, not router mode — safer toward visibility than
    silent pass-through.

    Pins the contract: if we ever change the default, the
    observed-issues entry on the Aider collision documents
    why we should keep it this way."""
    captured: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return make_response(content="ok")

    monkeypatch.setattr("gateway.router.litellm.acompletion", fake)

    # No X-FITT-Client header; no client: tag on the test
    # token. Defaults to "webui" (least trust) which is NOT
    # router mode.
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "fitt-smart",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=_auth(),
    )
    assert r.status_code == 200, r.text

    # System prefix present — confirms agent mode, not router.
    system_msgs = [m for m in captured[0]["messages"] if m["role"] == "system"]
    assert system_msgs, "unknown client should default to agent mode (with system prefix)"


# --------------------------------------------------------------- helper


def test_is_router_mode_client_contract() -> None:
    """Pin the contract on which client tags flip router mode.
    Guards against a naive equality check spreading across the
    codebase — the auth module is the single source of truth."""
    from gateway.auth import is_router_mode_client

    assert is_router_mode_client("coding-cli") is True
    for tag in ("ide", "telegram", "webui", "cli", "unknown", ""):
        assert is_router_mode_client(tag) is False, tag

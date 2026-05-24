"""Tests for the dashboard's view layer (Phase 7 Slice 7.5
Tasks 23-27). Today covers the overview page, the placeholder
views for not-yet-shipped pages, and the static asset mount.

The overview view is the centerpiece of the foundation slice
because it exercises every aggregation hook the dashboard
will use later (alias listing, context windows, probe results,
MCP, cron, gaps, pruners, telegram). Subsequent slices add
specialised views without rebuilding this scaffolding.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.alias_probe import ProbeResult
from gateway.app import create_app
from gateway.context_window import ContextWindowResult

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    return TestClient(app, follow_redirects=False)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# --------------------------------------------------------------- overview


def test_overview_renders_with_minimal_state(client: TestClient) -> None:
    """The overview page must render on a fresh gateway with
    no probe data, no context cache, no MCP servers, no cron
    jobs — just the configured aliases."""
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Headline metrics rendered.
    assert "Uptime" in body
    assert "Aliases" in body
    assert "MCP servers" in body
    assert "Cron jobs" in body
    # Configured aliases each get a row.
    assert "fitt-default" in body
    assert "fitt-smart" in body
    assert "fitt-fast" in body


def test_overview_surfaces_context_window(client: TestClient) -> None:
    """Cached context window from Slice 7.1 surfaces in the
    overview's alias table."""
    cache = client.app.state.context_windows
    cache._results[("ollama", "qwen-big")] = ContextWindowResult(
        tokens=32_768,
        source="modelfile",
        detail="num_ctx 32768",
        discovered_at=1779479823.42,
    )
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    # The renderer formats 32_768 as "32k".
    assert "32k" in r.text


def test_overview_surfaces_probe_status(client: TestClient) -> None:
    """The 'pip' status indicator reflects the boot probe's
    last result for each alias."""
    client.app.state.alias_probe_results = {
        "fitt-default": ProbeResult(
            alias="fitt-default",
            status="ok",
            detail="emitted 1 tool call(s) as expected",
            model_used="qwen2.5-coder:14b",
        )
    }
    client.app.state.alias_probe_ran_at = 1779479823.42
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    assert "pip-ok" in r.text


def test_overview_partial_returns_panel_only(client: TestClient) -> None:
    """The HTMX-driven partial endpoint returns just the panel
    fragment, not a full page. Used by ``hx-get`` polling for
    in-place refresh."""
    r = client.get("/dashboard/_partials/overview", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # The fragment renders the metrics grid but not the
    # base.html chrome.
    assert "Uptime" in body
    assert '<aside class="sidebar">' not in body  # no nav


def test_overview_redirects_without_auth(client: TestClient) -> None:
    """No bearer / no cookie still redirects to login."""
    r = client.get("/dashboard/")
    assert r.status_code == 302


# --------------------------------------------------------------- placeholder views


# --------------------------------------------------------------- placeholder views (none left)


def test_no_placeholder_left(client: TestClient) -> None:
    """Every sidebar link now resolves to a real view; no
    page should still render the "still pending" placeholder
    body."""
    for path in [
        "/dashboard/aliases",
        "/dashboard/turns",
        "/dashboard/tools",
        "/dashboard/cron",
        "/dashboard/audit",
        "/dashboard/health",
        "/dashboard/gaps",
    ]:
        r = client.get(path, headers=_auth())
        assert r.status_code == 200, f"{path} returned {r.status_code}"
        assert "still pending in Slice 7.5" not in r.text, (
            f"{path} still renders the placeholder body"
        )


# --------------------------------------------------------------- static


def test_static_css_served(client: TestClient) -> None:
    """The vendored stylesheet is reachable without auth (a
    missing CSS file would be a 404 every page would suffer)."""
    r = client.get("/dashboard/static/style.css")
    assert r.status_code == 200
    assert "FITT dashboard" in r.text  # comment header in style.css


def test_static_htmx_served(client: TestClient) -> None:
    """htmx.min.js is vendored locally — Tailscale-only
    deployments don't need internet access for the dashboard
    to work."""
    r = client.get("/dashboard/static/htmx.min.js")
    assert r.status_code == 200
    ct = r.headers["content-type"].lower()
    assert "javascript" in ct
    # The minified bundle starts with htmx's IIFE wrapper.
    assert "htmx" in r.text[:200].lower()


# --------------------------------------------------------------- nav highlight


def test_overview_nav_highlights_overview(client: TestClient) -> None:
    """The active nav class is on the Overview link, not on
    one of the others."""
    r = client.get("/dashboard/", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Cheap structural check: the Overview link is first in
    # the sidebar and carries the active class.
    overview_pos = body.index('href="/dashboard/"')
    aliases_pos = body.index('href="/dashboard/aliases"')
    assert overview_pos < aliases_pos
    # The active class must be on the overview link, not
    # the aliases link.
    overview_segment = body[overview_pos : overview_pos + 100]
    aliases_segment = body[aliases_pos : aliases_pos + 100]
    assert "active" in overview_segment
    assert "active" not in aliases_segment


# --------------------------------------------------------------- bearer in cookie context


def test_overview_works_with_session_cookie(client: TestClient) -> None:
    """After login via the form, the cookie alone is enough."""
    client.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )
    # No Authorization header — relying on the cookie jar.
    r = client.get("/dashboard/")
    assert r.status_code == 200
    assert "Uptime" in r.text


# --------------------------------------------------------------- aliases view


def test_aliases_view_lists_each_alias(client: TestClient) -> None:
    r = client.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "fitt-default" in body
    assert "fitt-smart" in body
    assert "fitt-fast" in body
    # Headers expected on the aliases table.
    assert "Last eval" in body
    assert "Dispatched 24h" in body


def test_aliases_view_surfaces_fallback(client: TestClient) -> None:
    """Aliases with a configured fallback render the fallback
    model id underneath the primary."""
    r = client.get("/dashboard/aliases", headers=_auth())
    body = r.text
    # fitt-default → qwen-big with fallback qwen-small (from
    # build_test_config).
    assert "fallback:" in body
    assert "qwen2.5-coder:7b" in body  # fallback model name


def test_aliases_view_surfaces_eval_pass_rate(tmp_path: Path) -> None:
    """When a rolling eval report exists, the aliases view
    renders the pass-rate badge."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    from gateway.alias_eval import default_eval_dir
    from gateway.config import fitt_home

    eval_dir = default_eval_dir(fitt_home())
    eval_dir.mkdir(parents=True, exist_ok=True)
    md = "\n".join(
        [
            "# Eval report — `fitt-smart`",
            "",
            "- Model: `anthropic/claude-sonnet-4.5`",
            "- Started: 2026-05-22T10:00:00",
            "- Finished: 2026-05-22T10:00:30",
            "- Duration: 30000 ms",
            "- Result: **4/5 passed** (80%)",
            "",
        ]
    )
    (eval_dir / "fitt-smart-latest.md").write_text(md, encoding="utf-8")

    r = tc.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    # The badge renders the pass-rate; check the literal text.
    assert "4/5 (80%)" in r.text


def test_aliases_view_surfaces_context_window_source(client: TestClient) -> None:
    cache = client.app.state.context_windows
    cache._results[("ollama", "qwen-big")] = ContextWindowResult(
        tokens=32_768,
        source="modelfile",
        detail="num_ctx 32768",
        discovered_at=1779479823.42,
    )
    r = client.get("/dashboard/aliases", headers=_auth())
    body = r.text
    assert "32k" in body
    assert "modelfile" in body  # source column


def test_aliases_partial_returns_table_only(client: TestClient) -> None:
    r = client.get("/dashboard/_partials/aliases", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "fitt-default" in body
    assert '<aside class="sidebar">' not in body  # no nav


def test_aliases_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/aliases")
    assert r.status_code == 302


# --------------------------------------------------------------- turns view


def _make_capture(
    turn_id: str = "turn-abc",
    *,
    session_key: str = "main",
    started_at: float = 1779479823.42,
    finished_at: float = 1779479825.81,
    narration_warning: bool = False,
    status: str = "ok",
    prompt_tokens: int = 5400,
    context_window: int | None = 32768,
    tool_calls: list | None = None,
):
    """Build a TurnCapture for tests. Mirrors the helper in
    test_turn_capture_endpoint.py — kept local rather than
    shared so changes to one test file don't surprise the
    other."""
    from gateway.turn_capture import TurnCapture

    pct = (prompt_tokens / context_window * 100.0) if context_window else None
    return TurnCapture(
        turn_id=turn_id,
        session_key=session_key,
        alias="fitt-default",
        client="telegram",
        model_used="qwen2.5-coder:14b",
        backend="ollama",
        fallback_used=False,
        started_at=started_at,
        finished_at=finished_at,
        dispatched_messages=[
            {"role": "system", "content": "[Capabilities]\nyou have these tools..."},
            {"role": "user", "content": "Read README.md"},
        ],
        response={
            "choices": [
                {
                    "message": {"role": "assistant", "content": "I'll read it."},
                    "finish_reason": "stop",
                }
            ]
        },
        tool_calls=tool_calls or [],
        prompt_tokens=prompt_tokens,
        completion_tokens=89,
        context_window=context_window,
        prompt_pct_of_window=pct,
        finish_reason="stop",
        narration_warning=narration_warning,
        iterations=1,
        status=status,
    )


def test_turns_list_renders_empty(tmp_path: Path) -> None:
    """A session with no captures renders the empty state, not
    a crash."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/turns", headers=_auth())
    assert r.status_code == 200
    assert "No captured turns" in r.text


def test_turns_list_shows_recent_captures(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    store.write(_make_capture("turn-aaa"))
    store.write(_make_capture("turn-bbb", started_at=1779479900.0, finished_at=1779479905.0))

    r = tc.get("/dashboard/turns", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "turn-aaa" in body
    assert "turn-bbb" in body
    # The list table renders the alias + model.
    assert "fitt-default" in body
    assert "qwen2.5-coder:14b" in body
    # Prompt + fill columns get rendered.
    assert "5k" in body  # prompt_tokens=5400 → "5k"
    assert "16%" in body  # prompt_pct_of_window=16.48 → "16%"


def test_turns_list_warns_on_narration(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    store.write(_make_capture("narrated", narration_warning=True))

    r = tc.get("/dashboard/turns", headers=_auth())
    assert r.status_code == 200
    # The narration badge is rendered for the row.
    assert "narration" in r.text.lower() or "⚠" in r.text


def test_turns_list_session_switcher(tmp_path: Path) -> None:
    """Sessions with captures appear in the available list so
    the operator can switch between them."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    store.write(_make_capture("aaa", session_key="main"))
    store.write(_make_capture("bbb", session_key="other"))

    r = tc.get("/dashboard/turns", headers=_auth())
    assert r.status_code == 200
    # Both session ids appear in the available-sessions footer.
    assert "main" in r.text
    assert "other" in r.text


def test_turns_list_takes_session_query(tmp_path: Path) -> None:
    """``?session=<x>`` switches the rendered list."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    store.write(_make_capture("only-other", session_key="other"))

    r = tc.get("/dashboard/turns?session=other", headers=_auth())
    assert r.status_code == 200
    assert "only-other" in r.text


def test_turns_detail_renders(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    store.write(_make_capture("turn-detail"))

    r = tc.get("/dashboard/turns/main/turn-detail", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Detail view renders the headline metrics.
    assert "Prompt tokens" in body
    assert "Completion" in body
    assert "Latency" in body
    # The dispatched-messages section renders the system + user.
    assert "[Capabilities]" in body
    assert "Read README.md" in body
    # Source pointer at the bottom.
    assert "captures/turn-detail" in body


def test_turns_detail_renders_tool_calls(tmp_path: Path) -> None:
    from gateway.turn_capture import CapturedToolCall

    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    cap = _make_capture(
        "turn-tools",
        tool_calls=[
            CapturedToolCall(
                call_id="c1",
                tool_name="read_file",
                args={"project": "fitt", "path": "README.md"},
                decision="auto",
                decision_detail="",
                duration_ms=12,
                ok=True,
                result_summary="contents...",
                artifact_path=None,
                iteration=0,
            )
        ],
    )
    store.write(cap)

    r = tc.get("/dashboard/turns/main/turn-tools", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "Tool calls" in body
    assert "read_file" in body
    assert "auto" in body
    assert "12ms" in body


def test_turns_detail_404_for_missing(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/turns/main/nonexistent", headers=_auth())
    assert r.status_code == 404
    assert "not found" in r.text.lower()


def test_turns_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/turns")
    assert r.status_code == 302
    r = client.get("/dashboard/turns/main")
    assert r.status_code == 302
    r = client.get("/dashboard/turns/main/abc")
    assert r.status_code == 302


def test_turns_session_path_traversal_safe(tmp_path: Path) -> None:
    """A session id with a path-traversal payload sanitises down
    to something benign — we never want a crafted URL to read
    files outside sessions/."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    # Try a couple of known-evil shapes. None should 500.
    for evil in ["..%2Fmain", "../../etc/passwd"]:
        r = tc.get(f"/dashboard/turns/{evil}", headers=_auth())
        assert r.status_code in (200, 404)


# --------------------------------------------------------------- tools view


def test_tools_view_lists_registered_tools(client: TestClient) -> None:
    r = client.get("/dashboard/tools", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # The registry has a known set of inline tools registered
    # by build_test_config's app — at minimum read_file / git_*.
    assert "read_file" in body
    # Bucket badges render.
    assert "Default bucket" in body


def test_tools_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/tools")
    assert r.status_code == 302


# --------------------------------------------------------------- cron view


def test_cron_view_renders_empty(client: TestClient) -> None:
    r = client.get("/dashboard/cron", headers=_auth())
    assert r.status_code == 200
    assert "No cron jobs configured" in r.text


def test_cron_view_lists_jobs(tmp_path: Path) -> None:
    """A configured cron job appears in the table with its
    schedule and message preview."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    cron_service = app.state.cron
    from gateway.cron import CronJob, CronSchedule

    job = CronJob(
        id="cj1",
        name="every-hour",
        message="Check the inbox",
        schedule=CronSchedule(kind="every", every_secs=3600),
        enabled=True,
        created_by_client="cli",
        created_ts=1779479823.42,
    )
    cron_service.add(job)

    r = tc.get("/dashboard/cron", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "every-hour" in body
    assert "every 1h" in body
    assert "Check the inbox" in body


# --------------------------------------------------------------- audit view


def test_audit_view_renders_empty(client: TestClient) -> None:
    r = client.get("/dashboard/audit", headers=_auth())
    assert r.status_code == 200
    assert "No audit entries" in r.text


def test_audit_view_lists_entries(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    audit = app.state.audit
    from gateway.audit import new_entry

    audit.append(
        new_entry(
            session_key="main",
            client="telegram",
            tool="read_file",
            args={"path": "README.md"},
            decision="auto",
            ok=True,
            duration_ms=12,
            ts=1779479823.42,
        )
    )

    r = tc.get("/dashboard/audit", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "read_file" in body
    assert "telegram" in body
    assert "auto" in body


def test_audit_view_filters_by_tool(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    audit = app.state.audit
    from gateway.audit import new_entry

    audit.append(
        new_entry(
            session_key="main",
            client="cli",
            tool="read_file",
            args={"path": "a"},
            decision="auto",
            ok=True,
        )
    )
    audit.append(
        new_entry(
            session_key="main",
            client="cli",
            tool="grep_repo",
            args={"pattern": "x"},
            decision="auto",
            ok=True,
        )
    )

    r = tc.get("/dashboard/audit?tool=read_file", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "read_file" in body
    # grep_repo appears as the filter input value but not in
    # the table body.
    assert body.count("grep_repo") == 0


# --------------------------------------------------------------- health view


def test_health_view_renders(client: TestClient) -> None:
    r = client.get("/dashboard/health", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "Gateway uptime" in body
    assert "MCP servers" in body
    assert "Cron" in body


def test_health_partial_returns_panel_only(client: TestClient) -> None:
    r = client.get("/dashboard/_partials/health", headers=_auth())
    assert r.status_code == 200
    assert '<aside class="sidebar">' not in r.text
    assert "Gateway uptime" in r.text


def test_health_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/health")
    assert r.status_code == 302


# --------------------------------------------------------------- gaps view


def test_gaps_view_renders_empty(client: TestClient) -> None:
    r = client.get("/dashboard/gaps", headers=_auth())
    assert r.status_code == 200
    assert "No capability gaps" in r.text


def test_gaps_view_renders_ranked_entries(tmp_path: Path) -> None:
    """A capability gap log gets surfaced ranked by frequency."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    gap_log = app.state.capability_gaps
    from gateway.capabilities import GapReport

    # Two distinct actions, the first one twice so it ranks
    # higher.
    gap_log.append(
        GapReport(
            ts=1779479823.42,
            session_key="main",
            action="run a docker container",
            suggestion="docker_run tool",
        )
    )
    gap_log.append(
        GapReport(
            ts=1779479900.0,
            session_key="main",
            action="run a docker container",
            suggestion="docker_run tool",
        )
    )
    gap_log.append(
        GapReport(
            ts=1779479950.0,
            session_key="main",
            action="send slack messages",
            suggestion="mcp.slack",
        )
    )

    r = tc.get("/dashboard/gaps", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "docker container" in body
    assert "slack messages" in body
    # The docker action appears before slack (count=2 vs 1).
    docker_pos = body.index("docker container")
    slack_pos = body.index("slack messages")
    assert docker_pos < slack_pos


# --------------------------------------------------------------- settings view


def test_settings_view_renders_aliases_and_models(client: TestClient) -> None:
    r = client.get("/dashboard/settings", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Aliases table renders.
    assert "fitt-default" in body
    assert "fitt-smart" in body
    # Models table renders.
    assert "qwen2.5-coder:14b" in body
    assert "anthropic/claude-sonnet-4.5" in body


def test_settings_redacts_secrets(client: TestClient) -> None:
    """Bearer tokens, API keys, and bot tokens MUST NEVER
    appear in the rendered HTML — even when the secrets
    object holds them. This is the load-bearing security
    contract of the read-only introspection commit."""
    r = client.get("/dashboard/settings", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # The fixture token must not leak.
    assert PERSONAL_TOKEN not in body
    # The fixture API key must not leak.
    assert "sk-or-test-xxxxx" not in body
    # Provider presence flags are visible instead.
    assert "openrouter" in body
    assert "configured" in body


def test_settings_renders_secrets_view_for_telegram(tmp_path: Path) -> None:
    """When telegram secrets are configured, the dashboard
    surfaces the allowlist user ids — they're not secrets,
    they're operator-configured identifiers — but not the
    bot token."""
    from gateway.config import (
        AllowedToken,
        Secrets,
        TelegramSecrets,
    )

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    cfg.secrets = Secrets(
        allowed_tokens=[AllowedToken(name="personal", token=PERSONAL_TOKEN)],
        openrouter_api_key="sk-or-test-xxxxx",
        telegram=TelegramSecrets(
            bot_token="123456:SUPER-SECRET-BOT-TOKEN-XYZ",
            allowlist_user_ids=[111, 222],
        ),
    )
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/settings", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Allowlist user ids are non-secret; render them.
    assert "111" in body
    assert "222" in body
    # Bot token is a secret. Never render it.
    assert "SUPER-SECRET-BOT-TOKEN" not in body
    assert "123456:" not in body


def test_settings_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/settings")
    assert r.status_code == 302


# --------------------------------------------------------------- projects view


def test_projects_view_renders_empty(client: TestClient) -> None:
    r = client.get("/dashboard/projects", headers=_auth())
    assert r.status_code == 200
    assert "No projects registered" in r.text


def test_projects_view_lists_registered(tmp_path: Path) -> None:
    from gateway.projects import Project, default_projects_path

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    # Populate the registry by writing the YAML directly; the
    # registry re-reads on every call.
    import yaml

    projects_path = default_projects_path()
    projects_path.parent.mkdir(parents=True, exist_ok=True)
    projects_path.write_text(
        yaml.safe_dump(
            {
                "projects": [
                    Project(
                        name="fitt",
                        path="/home/fred/src/fitt",
                        ssh_host="laptop.tailnet",
                        test_command="uv run pytest -q",
                    ).to_dict(),
                ]
            }
        ),
        encoding="utf-8",
    )

    r = tc.get("/dashboard/projects", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "fitt" in body
    assert "/home/fred/src/fitt" in body
    assert "laptop.tailnet" in body
    assert "uv run pytest -q" in body


# --------------------------------------------------------------- identity view


def test_identity_view_renders_empty_when_no_files(client: TestClient) -> None:
    """Memory disabled by default in build_test_config; the
    identity dir doesn't exist. The view renders all four
    expected files as empty rather than crashing."""
    r = client.get("/dashboard/identity", headers=_auth())
    assert r.status_code == 200
    body = r.text
    for name in ("user.md", "soul.md", "tools.md", "lessons.md"):
        assert name in body


def test_identity_view_renders_existing_files(tmp_path: Path) -> None:
    """When identity files exist, both rendered HTML and raw
    markdown are accessible in the view."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    identity_dir = cfg.memory.identity_dir
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "user.md").write_text(
        "# Who I am\n\nFred. I run **FITT**.\n",
        encoding="utf-8",
    )
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/identity", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Rendered markdown produces an <h1> for the heading.
    assert "Who I am" in body
    # Raw view contains the markdown source.
    assert "**FITT**" in body


def test_identity_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/identity")
    assert r.status_code == 302


# --------------------------------------------------------------- identity edit (F11)


def test_identity_edit_get_renders_form_for_existing_file(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    identity_dir = cfg.memory.identity_dir
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "user.md").write_text("# Who I am\n\nFred.\n", encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/identity/edit?filename=user.md", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Form contains the textarea pre-filled with current content.
    assert 'name="content"' in body
    assert "# Who I am" in body
    # CSRF token is present.
    assert 'name="csrf_token"' in body
    # Mtime hint populated.
    assert 'name="expected_mtime"' in body


def test_identity_edit_get_redirects_for_unknown_filename(client: TestClient) -> None:
    """Bogus filenames bounce to the listing rather than
    rendering a form."""
    r = client.get("/dashboard/identity/edit?filename=/etc/passwd", headers=_auth())
    assert r.status_code == 302
    assert "/dashboard/identity" in r.headers["location"]


def test_identity_edit_save_updates_file(tmp_path: Path) -> None:
    """Cookie + CSRF + mtime → file gets the new content."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    identity_dir = cfg.memory.identity_dir
    identity_dir.mkdir(parents=True, exist_ok=True)
    target = identity_dir / "user.md"
    target.write_text("old\n", encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    # Login to get a cookie.
    tc.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/identity"},
    )

    # Render the form to get a valid CSRF token + mtime.
    r = tc.get("/dashboard/identity/edit?filename=user.md")
    assert r.status_code == 200
    body = r.text
    # Extract csrf_token and expected_mtime from the rendered form.
    import re

    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', body)
    mtime_match = re.search(r'name="expected_mtime" value="([^"]+)"', body)
    assert csrf_match and mtime_match
    csrf_token = csrf_match.group(1)
    expected_mtime = mtime_match.group(1)

    # Submit the new content.
    r = tc.post(
        "/dashboard/identity/save",
        data={
            "csrf_token": csrf_token,
            "filename": "user.md",
            "expected_mtime": expected_mtime,
            "content": "new content\n",
        },
    )
    assert r.status_code == 200
    assert "Saved" in r.text
    # The file actually changed.
    assert target.read_text(encoding="utf-8") == "new content\n"


def test_identity_edit_save_rejects_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    identity_dir = cfg.memory.identity_dir
    identity_dir.mkdir(parents=True, exist_ok=True)
    target = identity_dir / "user.md"
    target.write_text("preserved\n", encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    tc.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/identity"},
    )

    r = tc.post(
        "/dashboard/identity/save",
        data={
            "csrf_token": "garbage",
            "filename": "user.md",
            "expected_mtime": "0",
            "content": "should not land\n",
        },
    )
    assert r.status_code == 403
    assert "CSRF" in r.text or "csrf" in r.text.lower()
    # File untouched.
    assert target.read_text(encoding="utf-8") == "preserved\n"


def test_identity_edit_save_rejects_unknown_filename(tmp_path: Path) -> None:
    """Even with a valid CSRF token, an unknown filename
    must not let the operator write outside the allowlist."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    tc.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/identity"},
    )

    # Get a valid token via a real form render.
    r = tc.get("/dashboard/identity/edit?filename=user.md")
    import re

    csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert csrf_match
    csrf_token = csrf_match.group(1)

    r = tc.post(
        "/dashboard/identity/save",
        data={
            "csrf_token": csrf_token,
            "filename": "../../../etc/passwd",
            "expected_mtime": "0",
            "content": "evil\n",
        },
    )
    assert r.status_code == 400
    assert "unknown identity file" in r.text


def test_identity_edit_save_emits_audit_entry(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    identity_dir = cfg.memory.identity_dir
    identity_dir.mkdir(parents=True, exist_ok=True)
    (identity_dir / "user.md").write_text("v1\n", encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    tc.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/identity"},
    )

    r = tc.get("/dashboard/identity/edit?filename=user.md")
    import re

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', r.text).group(1)
    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)
    tc.post(
        "/dashboard/identity/save",
        data={
            "csrf_token": csrf,
            "filename": "user.md",
            "expected_mtime": mtime,
            "content": "v2\n",
        },
    )

    audit = app.state.audit
    entries = audit.iter_entries()
    edit_entries = [e for e in entries if e.get("tool") == "dashboard.edit"]
    assert len(edit_entries) >= 1
    e = edit_entries[-1]
    assert e["ok"] is True
    assert e["decision"] == "approved"
    assert e["args"]["path"].endswith("user.md")


def test_identity_edit_save_redirects_without_auth(client: TestClient) -> None:
    r = client.post(
        "/dashboard/identity/save",
        data={
            "csrf_token": "x",
            "filename": "user.md",
            "expected_mtime": "0",
            "content": "anything",
        },
    )
    assert r.status_code == 302


# --------------------------------------------------------------- skills view


def test_skills_view_renders_empty(client: TestClient) -> None:
    r = client.get("/dashboard/skills", headers=_auth())
    assert r.status_code == 200
    assert "No skills loaded" in r.text


def test_skills_view_lists_loaded_skills(tmp_path: Path) -> None:
    """A loaded SKILL.md surfaces in the dashboard."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    skills_dir = cfg.memory.skills_dir
    skill_dir = skills_dir / "test-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: a smoke-test skill\n---\n# Body\n",
        encoding="utf-8",
    )

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/skills", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "test-skill" in body
    assert "smoke-test skill" in body


# --------------------------------------------------------------- sessions view


def test_sessions_view_renders_main(client: TestClient) -> None:
    """The session registry's ``main`` always exists."""
    r = client.get("/dashboard/sessions", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "main" in body


def test_sessions_view_shows_archived_state(tmp_path: Path) -> None:
    """Archived sessions render with an archived badge."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    registry = app.state.session_registry
    registry.create("retro", name="Retro project")
    registry.archive("retro")

    r = tc.get("/dashboard/sessions", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "retro" in body
    assert "archived" in body.lower()


# --------------------------------------------------------------- cost view


def test_cost_view_renders_empty(client: TestClient) -> None:
    r = client.get("/dashboard/cost", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # No log dir or no events → either renders the
    # "log directory does not exist" branch or the
    # "no events" branch.
    assert "Cost" in body


def test_cost_view_aggregates_completion_events(tmp_path: Path) -> None:
    """Writes a synthetic gateway.log line and confirms it
    aggregates into the per-month total."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    log_dir = Path(cfg.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    import json

    # Use a far-future month to avoid races with anything else.
    line = json.dumps(
        {
            "event": "chat.completion",
            "timestamp": "2026-05-22T12:00:00Z",
            "model": "qwen2.5-coder:14b",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cost_usd": "0.0123",
        }
    )
    (log_dir / "gateway.log").write_text(line + "\n", encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/cost?month=2026-05", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "qwen2.5-coder:14b" in body
    assert "1,000" in body or "1000" in body
    assert "$0.0123" in body or "0.0123" in body


def test_cost_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/cost")
    assert r.status_code == 302

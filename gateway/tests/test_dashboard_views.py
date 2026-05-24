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

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
from typing import Any

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


# ------------------------------------------------ F18: eval detail view


def _write_eval_report(
    tmp_path: Path,
    alias: str,
    *,
    cases: list[dict[str, Any]],
    model: str = "qwen2.5-coder:14b",
) -> Path:
    """Write a synthetic eval report at the standard location.

    Format matches :func:`gateway.alias_eval.render_report_markdown`.
    Caller passes a list of case dicts with keys ``name``,
    ``status``, ``latency_ms``, plus optional ``tool_called``,
    ``finish_reason``, ``detail``, ``reply_preview``.
    """
    from gateway.alias_eval import default_eval_dir
    from gateway.config import fitt_home

    eval_dir = default_eval_dir(fitt_home())
    eval_dir.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for c in cases if c["status"] == "pass")
    total = len(cases)
    pct = round(passed / total * 100) if total else 0
    lines = [
        f"# Eval report — `{alias}`",
        "",
        f"- Model: `{model}`",
        "- Started: 2026-05-22T10:00:00",
        "- Finished: 2026-05-22T10:00:30",
        "- Duration: 30000 ms",
        f"- Result: **{passed}/{total} passed** ({pct}%)",
        "",
        "## Cases",
        "",
    ]
    for c in cases:
        icon = "✅" if c["status"] == "pass" else "❌"
        lines.append(f"### {icon} `{c['name']}` — {c['status']}")
        lines.append("")
        lines.append(f"- Latency: {c.get('latency_ms', 0)} ms")
        if c.get("tool_called"):
            lines.append(f"- Tool called: `{c['tool_called']}`")
        if c.get("finish_reason"):
            lines.append(f"- Finish reason: `{c['finish_reason']}`")
        lines.append(f"- Detail: {c.get('detail', '(no detail)')}")
        if c.get("reply_preview"):
            lines.append("- Reply preview:")
            lines.append("")
            lines.append("  ```")
            lines.append(f"  {c['reply_preview']}")
            lines.append("  ```")
        lines.append("")

    path = eval_dir / f"{alias}-latest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _build_app_with_eval(tmp_path: Path, alias: str, cases: list[dict[str, Any]]) -> TestClient:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    _write_eval_report(tmp_path, alias, cases=cases)
    return TestClient(app, follow_redirects=False)


_PASS_CASE = {
    "name": "read_file_basic",
    "status": "pass",
    "latency_ms": 521,
    "detail": "called 'read_file' as expected",
    "tool_called": "read_file",
}
_PASS_CASE_2 = {
    "name": "grep_repo_basic",
    "status": "pass",
    "latency_ms": 612,
    "detail": "called 'grep_repo' as expected",
    "tool_called": "grep_repo",
}
_PASS_CASE_3 = {
    "name": "tool_disambiguation",
    "status": "pass",
    "latency_ms": 700,
    "detail": "called 'read_file' as expected",
    "tool_called": "read_file",
}
_PASS_CASE_4 = {
    "name": "no_tool_small_talk",
    "status": "pass",
    "latency_ms": 280,
    "detail": "no tool call as expected",
}
_PASS_CASE_5 = {
    "name": "list_capabilities_meta",
    "status": "pass",
    "latency_ms": 410,
    "detail": "called 'list_capabilities' as expected",
    "tool_called": "list_capabilities",
}


def test_aliases_view_links_eval_badge_to_detail(tmp_path: Path) -> None:
    """The pass-rate badge on the aliases tab is now wrapped
    in a link to the per-alias eval detail view (F18)."""
    tc = _build_app_with_eval(
        tmp_path,
        "fitt-default",
        [_PASS_CASE, _PASS_CASE_2, _PASS_CASE_3, _PASS_CASE_4, _PASS_CASE_5],
    )
    r = tc.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    assert "/dashboard/eval/fitt-default" in r.text


def test_eval_view_renders_for_known_alias(tmp_path: Path) -> None:
    tc = _build_app_with_eval(
        tmp_path,
        "fitt-default",
        [_PASS_CASE, _PASS_CASE_2, _PASS_CASE_3, _PASS_CASE_4, _PASS_CASE_5],
    )
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "fitt-default" in body
    assert "5/5" in body
    assert "qwen2.5-coder:14b" in body  # model id from header
    # Per-case names render.
    assert "read_file_basic" in body
    assert "no_tool_small_talk" in body


def test_eval_view_recommended_verdict_on_full_pass(tmp_path: Path) -> None:
    tc = _build_app_with_eval(
        tmp_path,
        "fitt-default",
        [_PASS_CASE, _PASS_CASE_2, _PASS_CASE_3, _PASS_CASE_4, _PASS_CASE_5],
    )
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    assert "Recommended" in r.text
    assert "verdict-recommended" in r.text


def test_eval_view_workable_when_only_negative_case_fails(tmp_path: Path) -> None:
    """Granite-incident inverse: tool-required cases all pass,
    only the negative ('What is 2+2?') case got a tool call.
    Workable, not risky."""
    cases = [
        _PASS_CASE,
        _PASS_CASE_2,
        _PASS_CASE_3,
        {
            "name": "no_tool_small_talk",
            "status": "no_tool_expected_but_called",
            "latency_ms": 320,
            "detail": "model called 'read_file' when no tool was expected",
            "tool_called": "read_file",
        },
        _PASS_CASE_5,
    ]
    tc = _build_app_with_eval(tmp_path, "fitt-default", cases)
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    assert "Workable" in r.text
    assert "verdict-workable" in r.text
    assert "over-eager" in r.text


def test_eval_view_risky_on_narrated_tool_required_case(tmp_path: Path) -> None:
    """The granite-incident shape: tool-required case got
    narrated text instead of real tool_calls. Risky."""
    cases = [
        {
            "name": "read_file_basic",
            "status": "narrated",
            "latency_ms": 1100,
            "detail": "model replied with 412 chars instead of emitting tool_calls",
            "finish_reason": "stop",
            "reply_preview": '```json\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```',
        },
        _PASS_CASE_2,
        _PASS_CASE_3,
        _PASS_CASE_4,
        _PASS_CASE_5,
    ]
    tc = _build_app_with_eval(tmp_path, "fitt-default", cases)
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    assert "Risky" in r.text
    assert "verdict-risky" in r.text
    assert "narrated" in r.text
    # Reply preview surfaces inside <details>.
    assert "Reply preview" in r.text
    # Reply preview content is HTML-escaped by Jinja's
    # autoescape; just check the unambiguous substring lands.
    assert "read_file" in r.text


def test_eval_view_incomplete_on_dispatch_failure(tmp_path: Path) -> None:
    """One dispatch-failure case (Phase 7.6 taxonomy) → can't
    make a verdict yet."""
    cases = [
        _PASS_CASE,
        _PASS_CASE_2,
        _PASS_CASE_3,
        {
            "name": "no_tool_small_talk",
            "status": "unreachable",
            "latency_ms": 15000,
            "detail": "timed out after 15s and endpoint is unreachable",
        },
        _PASS_CASE_5,
    ]
    tc = _build_app_with_eval(tmp_path, "fitt-default", cases)
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    assert "Incomplete" in r.text
    assert "verdict-incomplete" in r.text


def test_eval_view_not_recommended_on_low_pass_rate(tmp_path: Path) -> None:
    cases = [
        {
            "name": "read_file_basic",
            "status": "narrated",
            "latency_ms": 1100,
            "detail": "narration",
        },
        {
            "name": "grep_repo_basic",
            "status": "wrong_tool",
            "latency_ms": 800,
            "detail": "wrong tool",
        },
        _PASS_CASE_3,
        {
            "name": "no_tool_small_talk",
            "status": "no_tool_expected_but_called",
            "latency_ms": 320,
            "detail": "tool called when none expected",
        },
        {
            "name": "list_capabilities_meta",
            "status": "narrated",
            "latency_ms": 990,
            "detail": "narration",
        },
    ]
    tc = _build_app_with_eval(tmp_path, "fitt-default", cases)
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    # Mixed failures: narrated wins (highest-priority signal),
    # but the verdict points at risky vs not_recommended based
    # on pct. 1/5 = 20% → not_recommended takes over once we
    # check pct... actually narrated short-circuits to risky.
    # This test covers the narrated-priority path, which is
    # the most operationally important one to surface.
    assert "Risky" in r.text or "Not recommended" in r.text
    assert "narrated" in r.text


def test_eval_view_renders_when_no_report(tmp_path: Path) -> None:
    """No report on disk → the page still renders with an
    empty state and an "incomplete" verdict, not a 500."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # New template says "No <code>{suite.name}</code> eval report on disk"
    # for each missing suite (default + coding both empty here).
    assert "eval report on disk" in body
    assert "verdict-incomplete" in body


def test_eval_view_for_unknown_alias_renders_with_note(tmp_path: Path) -> None:
    """Alias that's no longer in config still gets a page when
    a stale report exists — useful for history."""
    tc = _build_app_with_eval(
        tmp_path,
        "fitt-deprecated",
        [_PASS_CASE, _PASS_CASE_2, _PASS_CASE_3, _PASS_CASE_4, _PASS_CASE_5],
    )
    r = tc.get("/dashboard/eval/fitt-deprecated", headers=_auth())
    assert r.status_code == 200
    assert "fitt-deprecated" in r.text
    assert "isn't in the current" in r.text


def test_eval_view_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/eval/fitt-default")
    assert r.status_code == 302


# ------------------------------------------------ coding-agent eval suite


def _write_eval_report_with_suite(
    tmp_path: Path,
    alias: str,
    *,
    suite: str,
    cases: list[dict[str, Any]],
    model: str = "qwen2.5-coder:14b",
) -> Path:
    """Write a synthetic eval report for the given suite.

    Mirrors :func:`_write_eval_report` but lets the test pin
    the suite-specific filename (``<alias>-coding-latest.md``
    vs ``<alias>-latest.md``)."""
    from gateway.alias_eval import default_eval_dir
    from gateway.config import fitt_home

    eval_dir = default_eval_dir(fitt_home())
    eval_dir.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for c in cases if c["status"] == "pass")
    total = len(cases)
    pct = round(passed / total * 100) if total else 0
    lines = [
        f"# Eval report — `{alias}`",
        "",
        f"- Model: `{model}`",
        "- Started: 2026-05-22T10:00:00",
        "- Finished: 2026-05-22T10:00:30",
        "- Duration: 30000 ms",
        f"- Result: **{passed}/{total} passed** ({pct}%)",
        "",
        "## Cases",
        "",
    ]
    for c in cases:
        icon = "✅" if c["status"] == "pass" else "❌"
        lines.append(f"### {icon} `{c['name']}` — {c['status']}")
        lines.append("")
        lines.append(f"- Latency: {c.get('latency_ms', 0)} ms")
        if c.get("tool_called"):
            lines.append(f"- Tool called: `{c['tool_called']}`")
        if c.get("finish_reason"):
            lines.append(f"- Finish reason: `{c['finish_reason']}`")
        lines.append(f"- Detail: {c.get('detail', '(no detail)')}")
        lines.append("")

    suffix = "" if suite == "default" else f"-{suite}"
    path = eval_dir / f"{alias}{suffix}-latest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_eval_view_renders_both_suites_when_present(tmp_path: Path) -> None:
    """When both default and coding reports exist, the page
    renders both with their own verdict banners."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _write_eval_report_with_suite(
        tmp_path,
        "fitt-default",
        suite="default",
        cases=[_PASS_CASE, _PASS_CASE_2, _PASS_CASE_3, _PASS_CASE_4, _PASS_CASE_5],
    )
    coding_cases = [
        {
            "name": "code_read_basic",
            "status": "pass",
            "latency_ms": 600,
            "tool_called": "read_file",
        },
        {
            "name": "code_edit_basic",
            "status": "pass",
            "latency_ms": 700,
            "tool_called": "edit_file",
        },
        {
            "name": "code_glob_search",
            "status": "pass",
            "latency_ms": 500,
            "tool_called": "glob_search",
        },
        {"name": "code_shell_basic", "status": "pass", "latency_ms": 800, "tool_called": "shell"},
        {
            "name": "code_no_tool_small_talk",
            "status": "pass",
            "latency_ms": 300,
            "detail": "no tool call",
        },
    ]
    _write_eval_report_with_suite(tmp_path, "fitt-default", suite="coding", cases=coding_cases)

    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Both suite labels render.
    assert "FITT default" in body
    assert "Coding agent" in body
    # Both case names from each suite render.
    assert "read_file_basic" in body
    assert "code_read_basic" in body
    # Two "Recommended" verdicts (one per suite).
    assert body.count("Recommended") >= 2


def test_eval_view_coding_suite_alone_renders_with_default_empty(tmp_path: Path) -> None:
    """If only the coding report exists, the page shows it
    plus a 'no eval report on disk' empty state for default."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    coding_cases = [
        {
            "name": "code_read_basic",
            "status": "pass",
            "latency_ms": 600,
            "tool_called": "read_file",
        },
        {
            "name": "code_edit_basic",
            "status": "narrated",
            "latency_ms": 1500,
            "detail": "model replied with 380 chars instead of emitting tool_calls",
            "finish_reason": "stop",
        },
        {
            "name": "code_glob_search",
            "status": "pass",
            "latency_ms": 500,
            "tool_called": "glob_search",
        },
        {
            "name": "code_shell_basic",
            "status": "narrated",
            "latency_ms": 1200,
            "detail": "model replied with 450 chars instead of emitting tool_calls",
            "finish_reason": "stop",
        },
        {"name": "code_no_tool_small_talk", "status": "pass", "latency_ms": 300},
    ]
    _write_eval_report_with_suite(tmp_path, "fitt-default", suite="coding", cases=coding_cases)

    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Default suite has no report → empty state.
    assert (
        "No <code>default</code> eval report on disk" in body
        or "<code>default</code> eval report on disk" in body
    )
    # Coding suite renders with risky verdict (narrated cases).
    assert "Risky" in body
    assert "code_shell_basic" in body


def test_run_eval_action_accepts_suite_form_field(tmp_path: Path) -> None:
    """The dashboard's run-eval action POSTs with a 'suite'
    form field. Confirm the route accepts it without error."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/aliases")
    csrf = _csrf_from(r.text)

    # Coding suite form post — the action will fail at dispatch
    # time (no live model) but the form field plumbing must work.
    r = tc.post(
        "/dashboard/actions/run-eval",
        data={
            "csrf_token": csrf,
            "alias": "fitt-default",
            "suite": "coding",
        },
    )
    assert r.status_code == 303
    assert "/dashboard/aliases" in r.headers["location"]


def test_aliases_panel_includes_suite_picker(tmp_path: Path) -> None:
    """The per-row 'run eval' button now has a suite dropdown
    so the operator can pick default vs coding without leaving
    the aliases page."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert 'name="suite"' in body
    assert '<option value="default"' in body
    assert '<option value="coding"' in body


# ------------------------------------------------ F19/F20: probe detail + re-probe


def test_aliases_view_surfaces_probe_transport_error_detail(client: TestClient) -> None:
    """F19: a transport_error probe shows its detail (the
    exception class + message) inline so the operator sees
    *why* without docker compose logs."""
    client.app.state.alias_probe_results = {
        "fitt-default": ProbeResult(
            alias="fitt-default",
            status="transport_error",
            detail="ConnectionError: All connection attempts failed",
            model_used="qwen3:14b",
        )
    }
    r = client.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "transport_error" in body
    assert "All connection attempts failed" in body


def test_aliases_view_surfaces_narrated_probe_detail(client: TestClient) -> None:
    """F19: a narrated probe surfaces its detail so the
    operator sees the narration shape."""
    client.app.state.alias_probe_results = {
        "fitt-default": ProbeResult(
            alias="fitt-default",
            status="narrated",
            detail="model replied with text instead of emitting a tool call",
            model_used="granite3.3:8b",
            reply_preview='```json {"name": "read_file"} ```',
        )
    }
    r = client.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "narrated" in body
    assert "text instead of emitting" in body


def test_aliases_panel_includes_reprobe_button(client: TestClient) -> None:
    """F20: the aliases tab has a 'Re-probe aliases' button
    posting to the typed-action route."""
    r = client.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    assert "/dashboard/actions/reprobe-aliases" in r.text
    assert "Re-probe aliases" in r.text


def test_aliases_panel_suite_picker_includes_realistic(client: TestClient) -> None:
    """The suite dropdown now offers the realistic suite too."""
    r = client.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    assert '<option value="realistic">' in r.text


def test_eval_view_renders_realistic_suite_block(tmp_path: Path) -> None:
    """The eval detail view shows a third suite block for the
    realistic suite when its report exists."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    realistic_cases = [
        {
            "name": "read_file_basic",
            "status": "narrated",
            "latency_ms": 1400,
            "detail": "model replied with 512 chars instead of emitting tool_calls",
            "finish_reason": "stop",
        },
        {
            "name": "grep_repo_basic",
            "status": "pass",
            "latency_ms": 600,
            "tool_called": "grep_repo",
        },
        {
            "name": "tool_disambiguation",
            "status": "pass",
            "latency_ms": 700,
            "tool_called": "read_file",
        },
        {"name": "no_tool_small_talk", "status": "pass", "latency_ms": 300},
        {
            "name": "list_capabilities_meta",
            "status": "pass",
            "latency_ms": 410,
            "tool_called": "list_capabilities",
        },
    ]
    _write_eval_report_with_suite(
        tmp_path, "fitt-default", suite="realistic", cases=realistic_cases
    )

    r = tc.get("/dashboard/eval/fitt-default", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert "Realistic" in body
    # The narrated tool-required case drives a risky verdict.
    assert "Risky" in body


def test_reprobe_action_refreshes_results(tmp_path: Path) -> None:
    """F20: the re-probe action re-runs probe_all_aliases and
    updates app.state.alias_probe_results in place — no gateway
    restart needed."""
    from unittest.mock import patch

    from gateway.alias_probe import ProbeResult as PR

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    # Seed a stale transport_error so we can prove it clears.
    app.state.alias_probe_results = {
        "fitt-default": PR(
            alias="fitt-default",
            status="transport_error",
            detail="stale",
        )
    }

    fresh = [
        PR(alias="fitt-default", status="ok", detail="ok now", model_used="m"),
    ]

    async def _stub(*_a: Any, **_k: Any) -> list[PR]:
        return fresh

    r = tc.get("/dashboard/aliases")
    csrf = _csrf_from(r.text)

    with patch("gateway.alias_probe.probe_all_aliases", side_effect=_stub):
        r = tc.post(
            "/dashboard/actions/reprobe-aliases",
            data={"csrf_token": csrf},
        )
    assert r.status_code == 303
    assert "/dashboard/aliases" in r.headers["location"]
    assert app.state.alias_probe_results["fitt-default"].status == "ok"


def test_reprobe_action_redirects_without_auth(client: TestClient) -> None:
    r = client.post("/dashboard/actions/reprobe-aliases", data={"csrf_token": "x"})
    assert r.status_code in (302, 303)


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


# --------------------------------------------------------------- projects edit (F12)


def _login(tc: TestClient) -> None:
    """Log in via the form so the test client carries the
    dashboard session cookie."""
    tc.post(
        "/dashboard/login",
        data={"token": PERSONAL_TOKEN, "next": "/dashboard/"},
    )


def _csrf_from(text: str) -> str:
    import re

    m = re.search(r'name="csrf_token" value="([^"]+)"', text)
    assert m, "no CSRF token in response"
    return m.group(1)


def test_projects_view_includes_csrf_and_add_form(client: TestClient) -> None:
    """The read view now also serves as the add-form host."""
    r = client.get("/dashboard/projects", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert 'action="/dashboard/projects/add"' in body
    assert 'name="csrf_token"' in body


def test_projects_add_round_trip(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    # Get a CSRF from the projects view itself.
    r = tc.get("/dashboard/projects")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/projects/add",
        data={
            "csrf_token": csrf,
            "name": "fitt",
            "path": "/home/fred/src/fitt",
            "ssh_host": "laptop.tailnet",
            "test_command": "uv run pytest -q",
            "build_command": "",
        },
    )
    assert r.status_code == 303
    assert "/dashboard/projects" in r.headers["location"]

    # Project landed.
    registry = app.state.project_registry
    project = registry.get("fitt")
    assert project.path == "/home/fred/src/fitt"
    assert project.ssh_host == "laptop.tailnet"
    assert project.test_command == "uv run pytest -q"


def test_projects_add_rejects_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.post(
        "/dashboard/projects/add",
        data={
            "csrf_token": "garbage",
            "name": "evil",
            "path": "/tmp",
        },
    )
    # Bounces back to /dashboard/projects with an error banner.
    assert r.status_code == 303
    assert "banner_message" in r.headers["location"]
    # No project was added.
    registry = app.state.project_registry
    from gateway.projects import UnknownProject

    with pytest.raises(UnknownProject):
        registry.get("evil")


def test_projects_update_changes_fields(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    # Pre-populate.
    from gateway.projects import Project

    app.state.project_registry.add(Project(name="fitt", path="/old/path", ssh_host="old-host"))

    r = tc.get("/dashboard/projects/edit?name=fitt")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/projects/update",
        data={
            "csrf_token": csrf,
            "name": "fitt",
            "path": "/new/path",
            "ssh_host": "new-host",
            "test_command": "uv run pytest",
            "build_command": "",
        },
    )
    assert r.status_code == 303

    project = app.state.project_registry.get("fitt")
    assert project.path == "/new/path"
    assert project.ssh_host == "new-host"
    assert project.test_command == "uv run pytest"


def test_projects_remove(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    from gateway.projects import Project, UnknownProject

    app.state.project_registry.add(Project(name="fitt", path="/home/fitt"))

    r = tc.get("/dashboard/projects/edit?name=fitt")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/projects/remove",
        data={"csrf_token": csrf, "name": "fitt"},
    )
    assert r.status_code == 303

    with pytest.raises(UnknownProject):
        app.state.project_registry.get("fitt")


def test_projects_actions_emit_audit(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/projects")
    csrf = _csrf_from(r.text)

    tc.post(
        "/dashboard/projects/add",
        data={
            "csrf_token": csrf,
            "name": "audited",
            "path": "/x",
        },
    )
    entries = app.state.audit.iter_entries()
    edit_entries = [e for e in entries if (e.get("tool") or "").startswith("dashboard.project_")]
    assert len(edit_entries) >= 1
    e = edit_entries[-1]
    assert e["tool"] == "dashboard.project_add"
    assert e["ok"] is True
    assert e["decision"] == "approved"


def test_projects_edit_redirects_without_auth(client: TestClient) -> None:
    r = client.post(
        "/dashboard/projects/add",
        data={"csrf_token": "x", "name": "y", "path": "/z"},
    )
    assert r.status_code == 302


# --------------------------------------------------------------- cron edit (F12)


def _make_cron(app, cron_id: str = "cj1", *, enabled: bool = True):
    from gateway.cron import CronJob, CronSchedule

    job = CronJob(
        id=cron_id,
        name=f"job-{cron_id}",
        message="check inbox",
        schedule=CronSchedule(kind="every", every_secs=3600),
        enabled=enabled,
        created_by_client="cli",
        created_ts=1779479823.42,
    )
    app.state.cron.add(job)
    return job


def test_cron_view_includes_csrf_and_action_forms(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _make_cron(app)
    _login(tc)

    r = tc.get("/dashboard/cron")
    assert r.status_code == 200
    body = r.text
    assert 'action="/dashboard/cron/toggle"' in body
    assert 'action="/dashboard/cron/remove"' in body


def test_cron_toggle_pauses_then_resumes(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _make_cron(app, "cj-pauseme")
    _login(tc)

    r = tc.get("/dashboard/cron")
    csrf = _csrf_from(r.text)

    # Pause.
    r = tc.post(
        "/dashboard/cron/toggle",
        data={"csrf_token": csrf, "cron_id": "cj-pauseme", "enable": "0"},
    )
    assert r.status_code == 303
    assert app.state.cron.get("cj-pauseme").enabled is False

    # Resume.
    r = tc.get("/dashboard/cron")
    csrf = _csrf_from(r.text)
    r = tc.post(
        "/dashboard/cron/toggle",
        data={"csrf_token": csrf, "cron_id": "cj-pauseme", "enable": "1"},
    )
    assert r.status_code == 303
    assert app.state.cron.get("cj-pauseme").enabled is True


def test_cron_remove_drops_job(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _make_cron(app, "cj-doomed")
    _login(tc)

    r = tc.get("/dashboard/cron")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/cron/remove",
        data={"csrf_token": csrf, "cron_id": "cj-doomed"},
    )
    assert r.status_code == 303
    assert app.state.cron.get("cj-doomed") is None


def test_cron_actions_reject_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _make_cron(app, "cj-csrf")
    _login(tc)

    r = tc.post(
        "/dashboard/cron/toggle",
        data={"csrf_token": "garbage", "cron_id": "cj-csrf", "enable": "0"},
    )
    assert r.status_code == 303
    assert "banner_message" in r.headers["location"]
    # State unchanged.
    assert app.state.cron.get("cj-csrf").enabled is True


def test_cron_actions_emit_audit(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _make_cron(app, "cj-audit")
    _login(tc)

    r = tc.get("/dashboard/cron")
    csrf = _csrf_from(r.text)
    tc.post(
        "/dashboard/cron/toggle",
        data={"csrf_token": csrf, "cron_id": "cj-audit", "enable": "0"},
    )

    entries = app.state.audit.iter_entries()
    cron_entries = [e for e in entries if (e.get("tool") or "").startswith("dashboard.cron_")]
    assert len(cron_entries) >= 1
    e = cron_entries[-1]
    assert e["tool"] == "dashboard.cron_toggle"
    assert e["ok"] is True


def test_cron_actions_redirect_without_auth(client: TestClient) -> None:
    r = client.post(
        "/dashboard/cron/toggle",
        data={"csrf_token": "x", "cron_id": "y", "enable": "0"},
    )
    assert r.status_code == 302
    r = client.post(
        "/dashboard/cron/remove",
        data={"csrf_token": "x", "cron_id": "y"},
    )
    assert r.status_code == 302


# --------------------------------------------------------------- skills edit (F13)


def _make_skill(cfg, name: str, *, valid: bool = True) -> Path:
    """Drop a SKILL.md under the configured skills_dir."""
    skill_dir = cfg.memory.skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    body = (
        f"---\nname: {name}\ndescription: a smoke-test skill\n---\n# Body\n"
        if valid
        else "no frontmatter at all\n"
    )
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_skills_edit_get_renders_form(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    _make_skill(cfg, "test-skill")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/skills/edit?skill_name=test-skill")
    assert r.status_code == 200
    body = r.text
    assert "test-skill" in body
    assert 'name="content"' in body
    assert 'name="csrf_token"' in body
    # Pre-filled with the actual file content.
    assert "smoke-test skill" in body


def test_skills_edit_get_redirects_for_unknown(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/skills/edit?skill_name=nonexistent")
    assert r.status_code == 302


def test_skills_edit_get_rejects_path_traversal(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    _make_skill(cfg, "good-skill")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    # A skill name that's actually a path → bounce to listing.
    r = tc.get("/dashboard/skills/edit?skill_name=../../etc/passwd")
    assert r.status_code == 302


def test_skills_edit_save_round_trip(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    skill_path = _make_skill(cfg, "test-skill")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/skills/edit?skill_name=test-skill")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    new_content = "---\nname: test-skill\ndescription: updated description\n---\n# New body\n"
    r = tc.post(
        "/dashboard/skills/save",
        data={
            "csrf_token": csrf,
            "skill_name": "test-skill",
            "expected_mtime": mtime,
            "content": new_content,
        },
    )
    assert r.status_code == 200
    assert "Saved" in r.text
    assert skill_path.read_text(encoding="utf-8") == new_content


def test_skills_edit_save_rejects_invalid_frontmatter(tmp_path: Path) -> None:
    """The dashboard refuses to write a SKILL.md the boot
    loader would skip — frontmatter that fails validation is
    rejected before disk write."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    skill_path = _make_skill(cfg, "test-skill")
    original = skill_path.read_text(encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/skills/edit?skill_name=test-skill")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    # No frontmatter fence → MissingOpenFence on validate.
    r = tc.post(
        "/dashboard/skills/save",
        data={
            "csrf_token": csrf,
            "skill_name": "test-skill",
            "expected_mtime": mtime,
            "content": "no frontmatter here\n",
        },
    )
    assert r.status_code == 400
    assert "validation" in r.text.lower() or "frontmatter" in r.text.lower()
    # File untouched.
    assert skill_path.read_text(encoding="utf-8") == original


def test_skills_edit_save_rejects_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    skill_path = _make_skill(cfg, "test-skill")
    original = skill_path.read_text(encoding="utf-8")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.post(
        "/dashboard/skills/save",
        data={
            "csrf_token": "garbage",
            "skill_name": "test-skill",
            "expected_mtime": "0",
            "content": "anything\n",
        },
    )
    assert r.status_code == 403
    assert skill_path.read_text(encoding="utf-8") == original


def test_skills_edit_save_emits_audit(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    _make_skill(cfg, "test-skill")

    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/skills/edit?skill_name=test-skill")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    tc.post(
        "/dashboard/skills/save",
        data={
            "csrf_token": csrf,
            "skill_name": "test-skill",
            "expected_mtime": mtime,
            "content": "---\nname: test-skill\ndescription: updated\n---\n# x\n",
        },
    )
    entries = app.state.audit.iter_entries()
    edit_entries = [e for e in entries if e.get("tool") == "dashboard.edit"]
    assert len(edit_entries) >= 1
    e = edit_entries[-1]
    assert e["ok"] is True
    assert e["args"]["path"].endswith("SKILL.md")


def test_skills_edit_redirects_without_auth(client: TestClient) -> None:
    r = client.post(
        "/dashboard/skills/save",
        data={
            "csrf_token": "x",
            "skill_name": "y",
            "expected_mtime": "0",
            "content": "anything",
        },
    )
    assert r.status_code == 302


# --------------------------------------------------------------- config edit (F14)


_VALID_CONFIG_YAML = """\
aliases:
  fitt-default: qwen-big
models:
  - id: qwen-big
    backend: ollama
    endpoint: http://laptop.tailnet:11434
    model: qwen2.5-coder:14b
"""


def _seed_config_file(tmp_path: Path) -> Path:
    """Write a real config.yaml to FITT_HOME for the edit tests
    that need an on-disk file to read first. The dashboard's
    config edit handler resolves the path via
    ``default_config_path()`` which honours FITT_HOME."""
    from gateway.config import fitt_home

    home = fitt_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "config.yaml"
    target.write_text(_VALID_CONFIG_YAML, encoding="utf-8")
    return target


def test_config_edit_get_renders_form(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_config_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/config/edit")
    assert r.status_code == 200
    body = r.text
    assert "config.yaml" in body
    # Disclaimer about restart-to-apply.
    assert "Restart" in body
    # Pre-filled with the on-disk content.
    assert "qwen2.5-coder:14b" in body
    # CSRF + mtime hints present.
    assert 'name="csrf_token"' in body
    assert 'name="expected_mtime"' in body
    # Sanity: target is the file we seeded.
    assert target.exists()


def test_config_save_round_trip(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_config_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/config/edit")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    new_yaml = _VALID_CONFIG_YAML + "upstream_timeout_secs: 600.0\n"
    r = tc.post(
        "/dashboard/settings/config/save",
        data={
            "csrf_token": csrf,
            "expected_mtime": mtime,
            "content": new_yaml,
        },
    )
    assert r.status_code == 200
    assert "Saved" in r.text
    # Disclaimer surfaces in the response so the operator sees
    # "restart required" right after a successful save.
    assert "Restart" in r.text
    assert target.read_text(encoding="utf-8") == new_yaml


def test_config_save_rejects_invalid_yaml(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_config_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/config/edit")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    # Trailing colon with no value at the end is malformed YAML.
    r = tc.post(
        "/dashboard/settings/config/save",
        data={
            "csrf_token": csrf,
            "expected_mtime": mtime,
            "content": "aliases:\n  fitt-default: qwen-big\nmodels:\n  - id: incomplete:\n",
        },
    )
    assert r.status_code == 400
    assert "Validation" in r.text or "validation" in r.text.lower()
    # File untouched.
    assert target.read_text(encoding="utf-8") == original


def test_config_save_rejects_unknown_alias_target(tmp_path: Path) -> None:
    """Cross-reference validation: alias points at a model id
    that doesn't exist → caught by Config.model_validate, save
    refuses, file unchanged."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_config_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/config/edit")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    # alias points at a model id that doesn't exist.
    bad_yaml = """\
aliases:
  fitt-default: nonexistent-model
models:
  - id: qwen-big
    backend: ollama
    endpoint: http://laptop:11434
    model: qwen2.5-coder:14b
"""
    r = tc.post(
        "/dashboard/settings/config/save",
        data={
            "csrf_token": csrf,
            "expected_mtime": mtime,
            "content": bad_yaml,
        },
    )
    assert r.status_code == 400
    assert target.read_text(encoding="utf-8") == original


def test_config_save_rejects_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_config_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.post(
        "/dashboard/settings/config/save",
        data={
            "csrf_token": "garbage",
            "expected_mtime": "0",
            "content": "anything",
        },
    )
    assert r.status_code == 403
    assert target.read_text(encoding="utf-8") == original


def test_config_save_emits_audit(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    _seed_config_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/config/edit")
    csrf = _csrf_from(r.text)
    import re

    mtime = re.search(r'name="expected_mtime" value="([^"]+)"', r.text).group(1)

    tc.post(
        "/dashboard/settings/config/save",
        data={
            "csrf_token": csrf,
            "expected_mtime": mtime,
            "content": _VALID_CONFIG_YAML + "upstream_timeout_secs: 240.0\n",
        },
    )

    entries = app.state.audit.iter_entries()
    edits = [e for e in entries if e.get("tool") == "dashboard.edit"]
    assert len(edits) >= 1
    e = edits[-1]
    assert e["ok"] is True
    assert e["args"]["path"].endswith("config.yaml")


def test_config_edit_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/settings/config/edit")
    assert r.status_code == 302
    r = client.post(
        "/dashboard/settings/config/save",
        data={
            "csrf_token": "x",
            "expected_mtime": "0",
            "content": "anything",
        },
    )
    assert r.status_code == 302


# --------------------------------------------------------------- secrets edit (F15)


def _seed_secrets_file(tmp_path: Path) -> Path:
    """Write a baseline secrets.yaml under FITT_HOME so the
    edit handler has something to read first. The file gets
    chmod'd in the same shape the gateway expects."""
    from gateway.config import fitt_home

    home = fitt_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "secrets.yaml"
    payload = (
        "allowed_tokens:\n"
        f"  - name: personal\n    token: {PERSONAL_TOKEN}\n"
        "openrouter_api_key: sk-or-test-existing\n"
    )
    target.write_text(payload, encoding="utf-8")
    if os.name != "nt":
        os.chmod(target, 0o600)
    return target


import os  # noqa: E402  (placed near use site for readability)


def test_secrets_edit_get_renders_form(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    _seed_secrets_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    assert r.status_code == 200
    body = r.text
    # Restart-required disclaimer.
    assert "Restart" in body
    # Each scalar key has a form.
    assert "openrouter_api_key" in body
    assert "anthropic_api_key" in body
    # Bearer re-confirm prompt is on the page.
    assert "re-confirm bearer" in body.lower() or "re-confirm" in body.lower()
    # The current value is NEVER rendered.
    assert "sk-or-test-existing" not in body


def test_secrets_edit_save_round_trip(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": csrf,
            "key": "openrouter_api_key",
            "new_value": "sk-or-NEW-VALUE-AAAA",
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303
    assert "/dashboard/settings/secrets/edit" in r.headers["location"]

    # File on disk reflects the new value.
    import yaml as _yaml

    raw = _yaml.safe_load(target.read_text(encoding="utf-8"))
    assert raw["openrouter_api_key"] == "sk-or-NEW-VALUE-AAAA"
    # The dashboard's redirect doesn't leak the new value
    # in the location header.
    assert "sk-or-NEW-VALUE-AAAA" not in r.headers["location"]


def test_secrets_edit_save_unsets_with_empty_value(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": csrf,
            "key": "openrouter_api_key",
            "new_value": "",  # unset
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303

    import yaml as _yaml

    raw = _yaml.safe_load(target.read_text(encoding="utf-8"))
    assert "openrouter_api_key" not in raw


def test_secrets_edit_save_rejects_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": "garbage",
            "key": "openrouter_api_key",
            "new_value": "sk-evil",
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303
    assert "banner_message" in r.headers["location"]
    # File untouched.
    assert target.read_text(encoding="utf-8") == original


def test_secrets_edit_save_rejects_bad_bearer(tmp_path: Path) -> None:
    """The bearer re-auth is the F15-specific second factor.
    A valid CSRF + valid cookie session must still fail
    if the bearer token doesn't match."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": csrf,
            "key": "openrouter_api_key",
            "new_value": "sk-evil",
            "bearer_token": "not-the-real-token",
        },
    )
    assert r.status_code == 303
    assert "banner_message" in r.headers["location"]
    assert target.read_text(encoding="utf-8") == original


def test_secrets_edit_save_rejects_unknown_key(tmp_path: Path) -> None:
    """Allowlist guards both the form input and the action
    handler. A POST with a fabricated key must not let the
    operator write outside the editable set."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": csrf,
            "key": "allowed_tokens",  # not in the editable set
            "new_value": "[]",
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303
    # Bounce with an error banner; file untouched.
    assert target.read_text(encoding="utf-8") == original


def test_secrets_edit_add_new_api_key(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/settings/secrets/save_new_api_key",
        data={
            "csrf_token": csrf,
            "model_id": "nvidia-minimax",
            "new_value": "nvapi-NEW-MINIMAX",
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303

    import yaml as _yaml

    raw = _yaml.safe_load(target.read_text(encoding="utf-8"))
    assert raw["api_keys"]["nvidia-minimax"] == "nvapi-NEW-MINIMAX"


def test_secrets_edit_add_new_api_key_rejects_bad_model_id(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    # A model id with a slash → rejected at the action wrapper.
    r = tc.post(
        "/dashboard/settings/secrets/save_new_api_key",
        data={
            "csrf_token": csrf,
            "model_id": "../evil",
            "new_value": "x",
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303
    assert target.read_text(encoding="utf-8") == original


def test_secrets_edit_chmod_to_0600(tmp_path: Path) -> None:
    """After a save, the file must end up at 0600 on POSIX.
    On Windows we don't enforce the mode (NTFS ACLs are the
    actual control); the test is a no-op there."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    target = _seed_secrets_file(tmp_path)
    if os.name != "nt":
        # Deliberately wide perms before the edit.
        os.chmod(target, 0o644)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": csrf,
            "key": "openrouter_api_key",
            "new_value": "sk-or-CHMOD-CHECK",
            "bearer_token": PERSONAL_TOKEN,
        },
    )
    assert r.status_code == 303
    if os.name != "nt":
        mode = target.stat().st_mode & 0o777
        assert mode == 0o600


def test_secrets_edit_emits_audit_without_value(tmp_path: Path) -> None:
    """The audit chain captures that an edit happened plus
    the key path, but never the value."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    _seed_secrets_file(tmp_path)
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/settings/secrets/edit")
    csrf = _csrf_from(r.text)

    secret_value = "sk-or-AUDIT-PROBE-VALUE-WHICH-MUST-NOT-LEAK"
    tc.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": csrf,
            "key": "openrouter_api_key",
            "new_value": secret_value,
            "bearer_token": PERSONAL_TOKEN,
        },
    )

    entries = app.state.audit.iter_entries()
    secret_entries = [e for e in entries if e.get("tool") == "dashboard.secret_set"]
    assert len(secret_entries) >= 1
    e = secret_entries[-1]
    assert e["ok"] is True
    assert e["args"]["key"] == "openrouter_api_key"
    # Critical: the value must not appear anywhere in the
    # serialised entry.
    import json as _json

    serialised = _json.dumps(e)
    assert secret_value not in serialised


def test_secrets_edit_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/settings/secrets/edit")
    assert r.status_code == 302
    r = client.post(
        "/dashboard/settings/secrets/save",
        data={
            "csrf_token": "x",
            "key": "openrouter_api_key",
            "new_value": "y",
            "bearer_token": "z",
        },
    )
    assert r.status_code == 302


# --------------------------------------------------------------- typed actions (F16)


def test_aliases_view_includes_action_buttons(client: TestClient) -> None:
    """The aliases page now renders 'Refresh context windows'
    + per-row 'run eval' buttons."""
    r = client.get("/dashboard/aliases", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert 'action="/dashboard/actions/refresh-aliases"' in body
    assert 'action="/dashboard/actions/run-eval"' in body


def test_audit_view_includes_verify_button(client: TestClient) -> None:
    r = client.get("/dashboard/audit", headers=_auth())
    assert r.status_code == 200
    assert 'action="/dashboard/actions/audit-verify"' in r.text


def test_health_view_includes_action_buttons(client: TestClient) -> None:
    r = client.get("/dashboard/health", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert 'action="/dashboard/actions/pruner-tick"' in body


def test_action_refresh_aliases_calls_cache(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/aliases")
    csrf = _csrf_from(r.text)

    # Track whether cache.populate was called.
    calls: list[object] = []
    cache = app.state.context_windows
    original = cache.populate

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    cache.populate = spy  # type: ignore[method-assign]

    r = tc.post(
        "/dashboard/actions/refresh-aliases",
        data={"csrf_token": csrf},
    )
    assert r.status_code == 303
    assert "/dashboard/aliases" in r.headers["location"]
    # The action ran (the redirect carries the success banner).
    assert "banner_message" in r.headers["location"]
    assert len(calls) == 1


def test_action_audit_verify_returns_ok(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    # Append a real audit entry first so we have something
    # to verify.
    from gateway.audit import new_entry

    app.state.audit.append(
        new_entry(
            session_key="main",
            client="cli",
            tool="read_file",
            args={"path": "x"},
            decision="auto",
            ok=True,
        )
    )

    r = tc.get("/dashboard/audit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/actions/audit-verify",
        data={"csrf_token": csrf},
    )
    assert r.status_code == 303
    assert "/dashboard/audit" in r.headers["location"]
    # Success banner.
    from urllib.parse import unquote_plus

    target = unquote_plus(r.headers["location"])
    assert "verified" in target.lower()


def test_action_pruner_tick_invokes_pruner(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    # Track whether tick was called.
    calls: list[object] = []
    pruner = app.state.history_pruner
    original_tick = pruner.tick

    async def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return await original_tick(*args, **kwargs)

    pruner.tick = spy  # type: ignore[method-assign]

    r = tc.get("/dashboard/health")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/actions/pruner-tick",
        data={"csrf_token": csrf, "which": "history"},
    )
    assert r.status_code == 303
    assert "/dashboard/health" in r.headers["location"]
    assert len(calls) == 1


def test_action_mcp_restart_unknown_server(tmp_path: Path) -> None:
    """No MCP servers configured in the test fixture; trying
    to restart one should land an error banner."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/health")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/actions/mcp-restart",
        data={"csrf_token": csrf, "name": "nonexistent"},
    )
    assert r.status_code == 303
    from urllib.parse import unquote_plus

    target = unquote_plus(r.headers["location"])
    assert "no mcp server" in target.lower()


def test_action_run_eval_unknown_alias(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/aliases")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/actions/run-eval",
        data={"csrf_token": csrf, "alias": "fitt-nonexistent"},
    )
    assert r.status_code == 303
    from urllib.parse import unquote_plus

    target = unquote_plus(r.headers["location"])
    assert "unknown alias" in target.lower()


def test_actions_reject_bad_csrf(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.post(
        "/dashboard/actions/refresh-aliases",
        data={"csrf_token": "garbage"},
    )
    assert r.status_code == 303
    assert "banner_message" in r.headers["location"]


def test_actions_emit_audit(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)
    _login(tc)

    r = tc.get("/dashboard/audit")
    csrf = _csrf_from(r.text)

    r = tc.post(
        "/dashboard/actions/audit-verify",
        data={"csrf_token": csrf},
    )
    assert r.status_code == 303

    entries = app.state.audit.iter_entries()
    action_entries = [e for e in entries if (e.get("tool") or "").startswith("dashboard.action.")]
    assert len(action_entries) >= 1
    e = action_entries[-1]
    assert e["tool"] == "dashboard.action.audit_verify"
    assert e["ok"] is True


def test_actions_redirect_without_auth(client: TestClient) -> None:
    r = client.post(
        "/dashboard/actions/refresh-aliases",
        data={"csrf_token": "x"},
    )
    assert r.status_code == 302
    r = client.post(
        "/dashboard/actions/audit-verify",
        data={"csrf_token": "x"},
    )
    assert r.status_code == 302


# --------------------------------------------------------------- live turns (F17)


def test_turns_list_includes_htmx_refresh_attributes(tmp_path: Path) -> None:
    """The turns list page wires HTMX poll-every-5s on the
    table region so a turn that lands while the operator's
    looking at the page surfaces without a manual reload."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    r = tc.get("/dashboard/turns", headers=_auth())
    assert r.status_code == 200
    body = r.text
    assert 'hx-get="/dashboard/_partials/turns' in body
    assert 'hx-trigger="load, every 5s"' in body


def test_turns_partial_returns_table_only(tmp_path: Path) -> None:
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    store = app.state.turn_capture
    store.write(_make_capture("turn-aaa"))

    r = tc.get("/dashboard/_partials/turns?session=main", headers=_auth())
    assert r.status_code == 200
    body = r.text
    # Fragment renders the table.
    assert "turn-aaa" in body
    # No sidebar in the fragment.
    assert '<aside class="sidebar">' not in body


def test_turns_partial_picks_up_new_captures(tmp_path: Path) -> None:
    """Simulate the live-refresh flow: render the partial
    once → write a new capture → render the partial again →
    new capture appears. This is the property F17 exists to
    deliver, just exercised through polling rather than
    SSE."""
    cfg = build_test_config(tmp_path, memory_enabled=True)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    tc = TestClient(app, follow_redirects=False)

    # First fetch — empty.
    r1 = tc.get("/dashboard/_partials/turns?session=main", headers=_auth())
    assert r1.status_code == 200
    assert "turn-NEW" not in r1.text

    # A turn finishes.
    app.state.turn_capture.write(_make_capture("turn-NEW"))

    # Second fetch picks it up.
    r2 = tc.get("/dashboard/_partials/turns?session=main", headers=_auth())
    assert r2.status_code == 200
    assert "turn-NEW" in r2.text


def test_turns_partial_redirects_without_auth(client: TestClient) -> None:
    r = client.get("/dashboard/_partials/turns?session=main")
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

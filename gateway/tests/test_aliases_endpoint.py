"""Tests for ``GET /v1/aliases`` — Phase 7 Slice 7.1.

Three concerns:

* Shape: every alias gets the documented payload (primary,
  fallback, context_window, last_probe, last_eval).
* Population: real cache data flows through; unknown aliases
  (no probe, no eval, no context window) get explicit nulls
  rather than dropped fields.
* Auth: the endpoint is bearer-gated by default. The
  OpenAI-compatible ``/v1/models`` stays auth-exempt for
  third-party-client compatibility; ``/v1/aliases`` is
  FITT-internal.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.alias_eval import EvalReport, default_eval_dir
from gateway.alias_probe import ProbeResult
from gateway.app import create_app
from gateway.config import fitt_home
from gateway.context_window import ContextWindowResult

from ._fixtures import PERSONAL_TOKEN, build_test_config


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    cfg = build_test_config(tmp_path)
    # boot-probe and context-window probes both poke the
    # network on startup; the test fixture disables both so
    # we can inject precise state per-test.
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {PERSONAL_TOKEN}"}


# --------------------------------------------------------------- auth


def test_aliases_requires_bearer(client: TestClient) -> None:
    r = client.get("/v1/aliases")
    assert r.status_code == 401


def test_aliases_returns_200_with_valid_bearer(client: TestClient) -> None:
    r = client.get("/v1/aliases", headers=_auth())
    assert r.status_code == 200


# --------------------------------------------------------------- shape


def test_aliases_shape_minimal(client: TestClient) -> None:
    """No probe / no eval / no context-window cache hits — the
    endpoint must still return one entry per configured alias
    with explicit nulls in the optional fields."""
    r = client.get("/v1/aliases", headers=_auth())
    body = r.json()

    assert "aliases" in body
    assert "generated_at" in body
    assert isinstance(body["generated_at"], (int, float))

    aliases = body["aliases"]
    # build_test_config defines fitt-default, fitt-smart, fitt-fast.
    ids = [a["id"] for a in aliases]
    assert sorted(ids) == ["fitt-default", "fitt-fast", "fitt-smart"]

    # Pick one and verify every field is present.
    smart = next(a for a in aliases if a["id"] == "fitt-smart")
    assert smart["primary"]["model_id"] == "openrouter-sonnet"
    assert smart["primary"]["model"] == "anthropic/claude-sonnet-4.5"
    assert smart["primary"]["backend"] == "openrouter"
    # No fallback configured for openrouter-sonnet in the fixture.
    assert smart["fallback"] is None
    # No probe / context / eval data wired -> nulls.
    assert smart["context_window"] is None
    assert smart["last_probe"] is None
    assert smart["last_eval"] is None


def test_aliases_includes_fallback_when_configured(client: TestClient) -> None:
    r = client.get("/v1/aliases", headers=_auth())
    body = r.json()
    default = next(a for a in body["aliases"] if a["id"] == "fitt-default")
    # fitt-default → qwen-big with fallback qwen-small.
    assert default["fallback"] is not None
    assert default["fallback"]["model_id"] == "qwen-small"
    assert default["fallback"]["backend"] == "ollama"


# --------------------------------------------------------------- context window


def test_aliases_surfaces_context_window_from_cache(tmp_path: Path) -> None:
    """When the context-window cache has results, /v1/aliases
    surfaces them per-alias with provenance."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)
    # Inject a result for one binding directly into the cache.
    cache = app.state.context_windows
    cache._results[("openrouter", "openrouter-sonnet")] = ContextWindowResult(
        tokens=200_000,
        source="lookup_table",
        detail="anthropic family match: 'claude-sonnet-4-5'",
        discovered_at=1779479823.42,
    )

    r = client.get("/v1/aliases", headers=_auth())
    body = r.json()
    smart = next(a for a in body["aliases"] if a["id"] == "fitt-smart")
    cw = smart["context_window"]
    assert cw is not None
    assert cw["tokens"] == 200_000
    assert cw["source"] == "lookup_table"
    assert "claude-sonnet-4-5" in cw["detail"]


# --------------------------------------------------------------- last probe


def test_aliases_surfaces_last_probe_when_present(tmp_path: Path) -> None:
    """When the boot probe ran for an alias, /v1/aliases
    shows the result. Includes the ran_at timestamp so
    operators can tell stale results from fresh ones."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)
    app.state.alias_probe_results = {
        "fitt-smart": ProbeResult(
            alias="fitt-smart",
            status="ok",
            detail="emitted 1 tool call(s) as expected",
            model_used="anthropic/claude-sonnet-4.5",
        )
    }
    app.state.alias_probe_ran_at = 1779479823.42

    r = client.get("/v1/aliases", headers=_auth())
    body = r.json()
    smart = next(a for a in body["aliases"] if a["id"] == "fitt-smart")
    probe = smart["last_probe"]
    assert probe is not None
    assert probe["status"] == "ok"
    assert probe["model_used"] == "anthropic/claude-sonnet-4.5"
    assert probe["ran_at"] == 1779479823.42

    # An alias the probe didn't cover stays None.
    default = next(a for a in body["aliases"] if a["id"] == "fitt-default")
    assert default["last_probe"] is None


# --------------------------------------------------------------- last eval


def test_aliases_surfaces_last_eval_when_present(tmp_path: Path) -> None:
    """When a rolling eval report exists at the canonical path,
    /v1/aliases parses its header for the pass-rate line."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    # Persist a real eval report via the existing helper —
    # ensures the parser is checked against the same format
    # the writer emits.
    started = datetime(2026, 5, 22, 10, 0, 0)
    finished = datetime(2026, 5, 22, 10, 0, 30)
    # EvalReport instance kept for documentation of what the
    # markdown below mimics; we write the markdown directly so
    # the test can pin a specific (passed, total) pair without
    # constructing matching CaseResult instances.
    _ = EvalReport(
        alias="fitt-smart",
        model_id="anthropic/claude-sonnet-4.5",
        started_at=started,
        finished_at=finished,
        cases=[],
    )
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

    r = client.get("/v1/aliases", headers=_auth())
    body = r.json()
    smart = next(a for a in body["aliases"] if a["id"] == "fitt-smart")
    eval_payload = smart["last_eval"]
    assert eval_payload is not None
    assert eval_payload["passed"] == 4
    assert eval_payload["total"] == 5
    assert eval_payload["pass_rate"] == pytest.approx(0.8)
    assert eval_payload["finished_at"] == "2026-05-22T10:00:30"

    # Other aliases without reports stay None.
    default = next(a for a in body["aliases"] if a["id"] == "fitt-default")
    assert default["last_eval"] is None


def test_aliases_handles_unparseable_eval(tmp_path: Path) -> None:
    """Garbled eval reports degrade to last_eval=null rather
    than failing the endpoint."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    eval_dir = default_eval_dir(fitt_home())
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "fitt-smart-latest.md").write_text("not a valid report", encoding="utf-8")

    r = client.get("/v1/aliases", headers=_auth())
    body = r.json()
    smart = next(a for a in body["aliases"] if a["id"] == "fitt-smart")
    assert smart["last_eval"] is None


# --------------------------------------------------------------- compatibility


def test_models_endpoint_still_works(client: TestClient) -> None:
    """Phase 7 must NOT break the OpenAI-compatible /v1/models
    surface; clients (Continue, Cursor, Open WebUI) depend on
    its shape and on it staying auth-exempt."""
    r = client.get("/v1/models")  # no auth header
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)


# --------------------------------------------------------------- refresh


def test_context_refresh_all_returns_refreshed_list(tmp_path: Path) -> None:
    """POST /v1/internal/context-refresh with no model_id
    re-runs discovery for every binding. Returns the new
    results inline so the CLI can render them."""
    import httpx
    import respx

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    body = {"parameters": "num_ctx 8192"}
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://laptop.tailnet:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        mock.post("http://localhost:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        mock.get("https://openrouter.ai/api/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [
                        {
                            "id": "anthropic/claude-sonnet-4.5",
                            "context_length": 200000,
                        },
                    ],
                },
            ),
        )

        r = client.post(
            "/v1/internal/context-refresh",
            json={},
            headers=_auth(),
        )
    assert r.status_code == 200
    payload = r.json()
    assert "refreshed" in payload
    refreshed = {entry["model_id"]: entry for entry in payload["refreshed"]}
    # All three bindings refreshed.
    assert "qwen-big" in refreshed
    assert "qwen-small" in refreshed
    assert "openrouter-sonnet" in refreshed
    assert refreshed["qwen-big"]["tokens"] == 8192
    assert refreshed["openrouter-sonnet"]["tokens"] == 200000


def test_context_refresh_single_alias(tmp_path: Path) -> None:
    """POST with model_id refreshes only that binding."""
    import httpx
    import respx

    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    body = {"parameters": "num_ctx 16384"}
    with respx.mock(assert_all_called=False) as mock:
        mock.post("http://laptop.tailnet:11434/api/show").mock(
            return_value=httpx.Response(200, json=body),
        )
        r = client.post(
            "/v1/internal/context-refresh",
            json={"model_id": "qwen-big"},
            headers=_auth(),
        )
    assert r.status_code == 200
    payload = r.json()
    assert len(payload["refreshed"]) == 1
    assert payload["refreshed"][0]["model_id"] == "qwen-big"
    assert payload["refreshed"][0]["tokens"] == 16384


def test_context_refresh_unknown_model_returns_error(tmp_path: Path) -> None:
    """POST with a model_id not in config returns a 200 with an
    error envelope (so the CLI can render it cleanly)."""
    cfg = build_test_config(tmp_path)
    cfg.server.boot_probe_enabled = False
    app = create_app(cfg)
    client = TestClient(app)

    r = client.post(
        "/v1/internal/context-refresh",
        json={"model_id": "nonexistent"},
        headers=_auth(),
    )
    assert r.status_code == 200
    payload = r.json()
    assert "error" in payload
    assert payload["error"]["type"] == "unknown_model"


def test_context_refresh_requires_bearer(client: TestClient) -> None:
    r = client.post("/v1/internal/context-refresh", json={})
    assert r.status_code == 401

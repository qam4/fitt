"""Smoke tests for Phase 4 app-state wiring.

`create_app` now attaches four Phase 4 objects to `app.state` so
the chat handler (Task 16) can reach them without threading them
through every request:

    - project_registry
    - execution_backend
    - tool_registry
    - approval

These aren't deep correctness tests — the individual subsystems
each have their own test files. What we're checking here is that
gateway startup builds the whole stack without raising and that
the tool registry comes up with the expected tool names so Task
16 can rely on them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import gateway.app as app_module
from gateway.app import create_app
from gateway.approval import ApprovalMiddleware
from gateway.capability_profile import CapabilityProfile, MeasuredGrade, write_profile
from gateway.config import AliasOrchestrationConfig
from gateway.projects import ProjectRegistry
from gateway.tools import ExecutionBackend, ToolRegistry

from ._fixtures import build_test_config


def _election_profile(alias: str, rate: float) -> CapabilityProfile:
    """A profile whose plan-election dimension carries ``rate``."""
    return CapabilityProfile(
        alias=alias,
        model_id="m",
        captured_at=datetime.now(UTC),
        measured=[
            MeasuredGrade(
                name="plan-election", pass_rate=rate, passes=int(rate * 5), valid=5, samples=5
            )
        ],
    )


def test_create_app_attaches_phase4_state(tmp_path: Path) -> None:
    """Build an app, make sure the Phase 4 state is bound and typed."""
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)

    assert isinstance(app.state.project_registry, ProjectRegistry)
    assert isinstance(app.state.execution_backend, ExecutionBackend)
    assert isinstance(app.state.tool_registry, ToolRegistry)
    assert isinstance(app.state.approval, ApprovalMiddleware)


def test_boot_warns_on_unsatisfied_feature(tmp_path: Path, monkeypatch) -> None:
    """12.5c task 14: an enabled feature the bound model can't drive
    (planning on a 0%-election profile) logs an ERROR at boot, mirroring
    check_missing_api_keys — and create_app still returns (never raises)."""
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    write_profile(_election_profile("fitt-default", 0.0), tmp_path)
    cfg = build_test_config(tmp_path)
    cfg.orchestration["fitt-default"] = AliasOrchestrationConfig(enabled=True)

    with patch.object(app_module, "_log") as mock_log:
        app = create_app(cfg)

    events = [call.args[0] for call in mock_log.error.call_args_list if call.args]
    assert "capability.feature_unsatisfied" in events
    assert app is not None  # boot completed despite the warning


def test_boot_silent_when_feature_satisfied(tmp_path: Path, monkeypatch) -> None:
    """A satisfied feature (100% election) logs no capability warning."""
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    write_profile(_election_profile("fitt-default", 1.0), tmp_path)
    cfg = build_test_config(tmp_path)
    cfg.orchestration["fitt-default"] = AliasOrchestrationConfig(enabled=True)

    with patch.object(app_module, "_log") as mock_log:
        create_app(cfg)

    events = [call.args[0] for call in mock_log.error.call_args_list if call.args]
    assert "capability.feature_unsatisfied" not in events


def test_tool_registry_is_preloaded(tmp_path: Path) -> None:
    """The inline + fileops + git tool groups are registered at startup.

    Task 16 dispatches by name; if a rename lands, this test tells
    us immediately that the chat handler needs updating.
    """
    cfg = build_test_config(tmp_path)
    app = create_app(cfg)

    names = set(app.state.tool_registry.list_names())
    # Every tool that should be available as of Task 7.
    expected = {
        # inline.py
        "list_capabilities",
        "spec_list",
        "spec_read",
        "spec_next_task",
        "spec_mark_task",
        # fileops.py
        "read_file",
        "list_directory",
        "grep_repo",
        "glob_search",
        # gitops.py
        "git_status",
        "git_diff",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"


def test_app_still_serves_health(tmp_path: Path) -> None:
    """The new wiring didn't break the gateway's most basic contract."""
    cfg = build_test_config(tmp_path)
    client = TestClient(create_app(cfg))
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

"""Shared pytest fixtures for the gateway test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to the tests/fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolate_fitt_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ~/.fitt to a pytest-managed tmp dir.

    This prevents tests from ever reading or writing to the real
    ~/.fitt directory on the developer's machine. Tests that need a
    specific fitt-home layout can still create files under the returned
    path.
    """
    fake_home = tmp_path / "fitt-home"
    fake_home.mkdir()
    monkeypatch.setenv("FITT_HOME", str(fake_home))
    # HOME/USERPROFILE override for code that uses Path.home() directly
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Skip the Phase 4.7 shell probe by default. Tests that
    # exercise project_shell or the probe itself unset or
    # override this. Avoids a ~2s subprocess tax on every
    # ``create_app`` call.
    monkeypatch.setenv("FITT_SKIP_SHELL_PROBE", "1")
    return fake_home

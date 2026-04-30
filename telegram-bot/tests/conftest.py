"""Shared pytest fixtures for the bot's test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_fitt_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect FITT_HOME to a pytest-managed tmp dir so the bot's
    config loader, prefs, and session registry all operate in
    isolation."""
    fake_home = tmp_path / "fitt-home"
    fake_home.mkdir()
    monkeypatch.setenv("FITT_HOME", str(fake_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return fake_home

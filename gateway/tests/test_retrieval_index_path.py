"""Where the retrieval index lives, and why it's configurable.

The e2e harness points identity_dir / sessions_dir at a temp run home,
but the index path used to resolve from FITT_HOME regardless — so eval
turns were indexed into the operator's real memory and could surface in
later recall. ``memory.index_path`` makes the redirect possible; these
tests pin both halves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gateway.config import Config, MemoryConfig, ModelConfig, ServerConfig
from gateway.retrieval.wiring import index_path


def _config(*, index: Path | None = None) -> Config:
    return Config(
        server=ServerConfig(),
        aliases={"fitt-embed": "embed-local"},
        models=[
            ModelConfig(
                id="embed-local",
                backend="ollama",
                endpoint="http://localhost:11434",
                model="nomic-embed-text:latest",
            )
        ],
        memory=MemoryConfig(embedding_alias="fitt-embed", index_path=index),
    )


def test_index_path_defaults_under_fitt_home(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))

    assert index_path(_config()) == tmp_path / "memory" / "index.db"


def test_configured_index_path_wins(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    iso = tmp_path / "run-home" / "memory" / "index.db"

    assert index_path(_config(index=iso)) == iso


def test_provider_is_built_at_the_configured_path(monkeypatch: Any, tmp_path: Path) -> None:
    """The isolation only holds if the provider honours it too."""
    monkeypatch.setenv("FITT_HOME", str(tmp_path))
    iso = tmp_path / "run-home" / "memory" / "index.db"

    from gateway.retrieval.wiring import build_retrieval_provider

    provider = build_retrieval_provider(_config(index=iso))

    assert provider is not None
    assert provider._db_path == iso


# ------------------------------------------- isolation, as a class
#
# Two separate leaks came from redirecting FITT_HOME-derived paths ad hoc:
# the retrieval index (eval turns landing in real memory) and skills_dir
# (the loader scanning the real dir, so a planted fixture was invisible
# and a working feature reported "the model never loaded the recipe").
# isolate_memory_paths enumerates them and asserts, so the next added
# path field fails loudly instead of leaking.


def test_isolate_memory_paths_redirects_every_path_field(tmp_path: Path) -> None:
    from gateway.e2e_driver import isolate_memory_paths

    run_home = tmp_path / "run"
    cfg = _config()

    isolate_memory_paths(cfg, run_home)

    for name, value in vars(cfg.memory).items():
        if isinstance(value, Path):
            assert run_home in value.parents, f"{name} escapes the run home: {value}"


def test_isolate_memory_paths_covers_skills_and_index(tmp_path: Path) -> None:
    """The two that actually leaked."""
    from gateway.e2e_driver import isolate_memory_paths

    run_home = tmp_path / "run"
    cfg = isolate_memory_paths(_config(), run_home)

    assert cfg.memory.skills_dir == run_home / "skills"
    assert cfg.memory.index_path == run_home / "memory" / "index.db"


def test_isolate_memory_paths_raises_on_an_unredirected_field(tmp_path: Path) -> None:
    """A newly added FITT_HOME-derived path must fail loudly here."""
    from gateway.e2e_driver import isolate_memory_paths

    run_home = tmp_path / "run"
    cfg = _config()
    isolate_memory_paths(cfg, run_home)
    # Simulate a field added later that nobody remembered to redirect.
    object.__setattr__(cfg.memory, "new_thing_dir", Path("/somewhere/else"))

    with pytest.raises(AssertionError, match="new_thing_dir"):
        isolate_memory_paths(cfg, run_home)

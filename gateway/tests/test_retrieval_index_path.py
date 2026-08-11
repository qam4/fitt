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
